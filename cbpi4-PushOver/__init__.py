# -*- coding: utf-8 -*-
"""Phone alerts through Pushover: one push for each thing the brew-day screen beeps for.

The brew-day screen sounds its alarm for a timer that has run out, again and
again until the timer is answered, and gives two beeps for each new alert that
takes its title bar: an addition, a question, an error, a warning. Each of
those reaches CraftBeerPi as a notification (NotificationController.notify),
and this sends a push for it from the server, so it arrives with no browser
open:

- a brew-day timer that has run out (TimersController: its state "done", its
  prompt titled with its name and worded "The 15:00 timer has run out."):
  emergency priority, which Pushover repeats every RETRY seconds for EXPIRE
  seconds until it is acknowledged on the phone. Once the timer is answered on
  any screen - OK, +5 MIN, set anew or removed - the repeats are cancelled
  through Pushover's receipts API, so the phone stops as the screens do;
- the boil's addition reminders, Hop Alert and First Wort Hop Addition!
  (BoilStep, and the runoff's first-wort hops in StepController): high
  priority;
- any other notification with buttons, a question waiting for an answer:
  high priority;
- an error or a warning with no buttons: high priority.

Info and success notifications make no sound on the screen and send nothing.

One push for each notification, and never another for it: a page that
reloads or reconnects is shown the prompts still waiting as they were, with no
new notification. After a restart, a timer brought back run out raises its
prompt again; its push was sent before and is still repeating, so it isn't
sent again. Which timers' pushes still repeat is kept in FILE, in the config
folder, for that. The same error or warning again within REPEAT_WINDOW
brewing seconds isn't sent again either: the screen shows the same words once.

Off until the user key and the application token are both filled in
(pushover_user and pushover_token in Settings): nothing is sent then, and
nothing is logged as an error. Each kind has its own switch, on by default. A
test alert: POST /pushover/test.

The Pushover API's address is CBPI_PUSHOVER_API in the environment, if set, so
a bench can point it at a fake; https://api.pushover.net/1 otherwise.

Sending never holds the brew up: each push goes out in its own task, through
aiohttp, which gives up after TIMEOUT seconds. A push that fails is said once
in the log, as a warning, and isn't tried again. Without the internet the
pushes are lost: the screens and the buzzer remain the alarms.

The key and the token never reach the log. What this writes leaves them out,
and a filter on the log's handlers replaces them wherever else they would
appear: CraftBeerPi writes every setting into the log at debug level.
"""

import asyncio
import importlib.metadata
import json
import logging
import os
import re
from collections import OrderedDict

import aiohttp
from aiohttp import web
from cbpi.api import CBPiExtension, clock, on_startup, request_mapping
from cbpi.api.config import ConfigType
from cbpi.api.persist import atomic_write_json
from cbpi.api.step import StepState

logger = logging.getLogger(__name__)

NAME = "cbpi4-PushOver"

#: Pushover's API, unless CBPI_PUSHOVER_API names another.
API = "https://api.pushover.net/1"
API_ENV = "CBPI_PUSHOVER_API"

#: Real seconds a request to Pushover may take before it is given up. The network's time, not the brewing clock's:
#: a simulation doesn't make the internet faster.
TIMEOUT = 10

#: A run-out timer's push: emergency priority, repeated by Pushover every RETRY real seconds for EXPIRE real seconds
#: until it is acknowledged on the phone, as the screen's alarm repeats until the timer is answered.
EMERGENCY, RETRY, EXPIRE = 2, 60, 3600
#: Everything else: high priority, one alert.
HIGH = 1

#: Brewing seconds within which the same error or warning isn't sent again.
REPEAT_WINDOW = 600
#: The most of those remembered at once.
REMEMBERED = 500

#: Pushover's limits on a title and a message, in characters.
TITLE_MAX, MESSAGE_MAX = 250, 1024

#: In the config folder: the timers whose pushes still repeat, so a restart can still cancel them.
FILE = "pushover.json"
FILE_VERSION = 1

USER, TOKEN = "pushover_user", "pushover_token"
KINDS = ("timers", "additions", "questions", "errors", "warnings")
SWITCH = {kind: "pushover_" + kind for kind in KINDS}
YES_NO = [{"label": "Yes", "value": "Yes"}, {"label": "No", "value": "No"}]

#: The settings: name, kind, choices, what it does. Each switch's "(default: Yes)" is what BLANK takes it as.
SETTINGS = (
    (USER, ConfigType.STRING, None,
     "Phone alerts: your Pushover user key, from the top of your dashboard on pushover.net once you've logged in. "
     "Alerts are sent only when this and pushover_token are both filled in. Blank: no alerts."),
    (TOKEN, ConfigType.STRING, None,
     "Phone alerts: the API token of the application you created for CraftBeerPi on pushover.net. Alerts are sent "
     "only when this and pushover_user are both filled in. Blank: no alerts."),
    (SWITCH["timers"], ConfigType.SELECT, YES_NO,
     "Phone alerts: a brew-day timer that has run out. An emergency alert, repeated every minute for up to an hour "
     "until you acknowledge it on the phone. Answering the timer on any screen stops it too. (default: Yes)"),
    (SWITCH["additions"], ConfigType.SELECT, YES_NO,
     "Phone alerts: a boil addition is due - the boil step's hop and first-wort reminders. One high-priority alert "
     "each. (default: Yes)"),
    (SWITCH["questions"], ConfigType.SELECT, YES_NO,
     "Phone alerts: CraftBeerPi asks something and waits for your answer on a screen. One high-priority alert each. "
     "(default: Yes)"),
    (SWITCH["errors"], ConfigType.SELECT, YES_NO,
     "Phone alerts: an error, such as a step that failed or an element left on by hand past its target. One "
     "high-priority alert each. (default: Yes)"),
    (SWITCH["warnings"], ConfigType.SELECT, YES_NO,
     "Phone alerts: a warning, which the brew-day screen beeps for too. One high-priority alert each. "
     "(default: Yes)"),
)

#: How TimersController words a timer's alarm, as the brew-day screen knows it (TIMER_RAN_OUT there).
TIMER_RAN_OUT = re.compile(r"The \d+(?::\d\d){1,2} timer has run out\.")
#: The titles of the boil's addition reminders: BoilStep's hop alerts and first-wort hops, and the runoff's
#: first-wort hops (StepController._first_wort_at_runoff).
ADDITIONS = ("Hop Alert", "First Wort Hop Addition!")
#: How those reminders are worded, and what the push says instead: what to do, and when.
ADDITION_WORDS = (
    (re.compile(r"Please add (?P<what>.+) \(\d+\)"), "add {what} now"),  # a hop alert: "Please add 1.50 oz Citra (1)"
    (re.compile(r"Please add Hop (?P<n>\d+)"), "add hop {n} now"),  # a hop alert with no text
    (re.compile(r"Please add (?P<what>.+) for first wort"), "add {what} for first wort now"),  # at the boil's start
    (re.compile(r"Please add (?P<what>.+)"), "add {what}"),  # the runoff's: "... before the first runnings reach it"
)
#: Titles that say nothing but the level: NOTIFY_ON_ERROR's, from the log, and "Error".
GENERIC = ("error", "warning", "critical")

TEST_TEXT = "Test alert from CraftBeerPi: phone alerts work."

#: A receipt as Pushover gives one, before it goes into an address.
RECEIPT = re.compile(r"[A-Za-z0-9]{1,64}")
#: The shortest key the log filter looks for: Pushover's are 30 characters, and hiding a short one would mangle lines.
HIDE_FROM = 8
HIDDEN = "[hidden]"


def _version():
    try:
        return importlib.metadata.version(NAME)
    except importlib.metadata.PackageNotFoundError:
        return "0.0.0"


def _plain(text):
    """Text on one line, its spaces as one."""
    return " ".join(str(text if text is not None else "").split())


def _join(title, message):
    """A notification's title and message as one line: "Boil: Is the wort in the boil kettle?", "Resume brew? ..."."""
    if not title or not message:
        return title or message
    return title + (" " if title[-1] in "?!.:" else ": ") + message


def _cut(text, most):
    return text if len(text) <= most else text[: most - 1] + "\u2026"


def _hide(text, secrets):
    for secret in secrets:
        text = text.replace(secret, HIDDEN)
    return text


class Alarm:
    """A timer's emergency push while it repeats on the phone: the timer's id and name, the prompt that rang it, and
    Pushover's receipt for it - None until Pushover answers, or if it didn't."""

    __slots__ = ("timer", "name", "prompt", "receipt")

    def __init__(self, timer, name, prompt, receipt=None):
        self.timer, self.name, self.prompt, self.receipt = timer, name, prompt, receipt

    def to_dict(self):
        return dict(timer=self.timer, name=self.name, prompt=self.prompt, receipt=self.receipt)


class _HideKeys(logging.Filter):
    """Replaces the user key and the application token in each line before a handler writes it, wherever the line
    comes from: CraftBeerPi logs every setting at debug level (the websocket's broadcast lines)."""

    def __init__(self, plugin):
        super().__init__()
        self.plugin = plugin

    @classmethod
    def install(cls, plugin):
        """One on each of the root logger's handlers, cbpi.log's and the console's, as log_repeats puts its own."""
        hide = cls(plugin)
        for handler in logging.getLogger().handlers:
            handler.addFilter(hide)
        return hide

    def filter(self, record):
        secrets = self.plugin._secrets()
        if not secrets:
            return True
        try:
            text = record.getMessage()
        except Exception:  # noqa: BLE001 - logging reports its own formatting errors
            return True
        hidden = _hide(text, secrets)
        if hidden != text:
            record.msg, record.args = hidden, None
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = _hide(record.exc_text, secrets)
        if record.stack_info:
            record.stack_info = _hide(record.stack_info, secrets)
        return True


class PushOver(CBPiExtension):

    #: What each setting left blank is taken as (blank_defaults): each kind's switch is on. The user key and the token
    #: aren't here: blank, they turn the alerts off, which isn't a value to fill in.
    BLANK = {SWITCH[kind]: "Yes" for kind in KINDS}

    @classmethod
    def blank_defaults(cls, unit=None):
        """What each setting left blank is taken as: BLANK, which the plugin reads as well, so the two can't drift
        apart. None depends on the temperature unit."""
        return dict(cls.BLANK)

    def __init__(self, cbpi):
        self.cbpi = cbpi
        self.version = _version()
        self.api = (os.environ.get(API_ENV) or "").strip().rstrip("/") or API
        self._session = None
        # The errors and warnings sent lately, oldest first: (kind, title, message) -> clock.monotonic() then.
        self._sent = OrderedDict()
        # Timer id -> its Alarm, while its push repeats on the phone.
        self._alarms = {}
        self._watcher = None
        # True once the timers are back after a start (_restored): only then does a missing timer mean its alarm is
        # over.
        self._ready = False
        folder = getattr(cbpi, "config_folder", None)
        self._path = folder.get_file_path(FILE) if folder is not None else None
        self._load()
        self._hiding = _HideKeys.install(self)
        # Heard from the start, before the steps and the timers are brought back: "Resume brew?" and a timer brought
        # back run out are raised as CraftBeerPi starts.
        cbpi.notification.add_listener(self._heard)
        cbpi.register(self, url_prefix="/pushover")
        app = getattr(cbpi, "app", None)
        if app is not None:
            app.on_cleanup.append(self._close)
        self._declaring = asyncio.get_running_loop().create_task(self._declare())

    # -- the settings ---------------------------------------------------------------------------------------------

    async def _declare(self):
        """The settings: added when missing, and brought up to date - descriptions, choices - with each new version.
        A switch left blank, or set to what it doesn't offer, is given what it is taken as (BLANK), so Settings shows
        what is in force. Never raises: without its settings the plugin is off."""
        try:
            renew = self.cbpi.config.get(NAME + "_update", None) != self.version
            for name, kind, options, description in SETTINGS:
                entry = self.cbpi.config.cache.get(name)
                if entry is not None and not renew:
                    continue
                value = "" if entry is None or entry.value is None else entry.value
                if options and value not in [option["value"] for option in options]:
                    value = self.BLANK[name]
                await self.cbpi.config.add(name, value, type=kind, description=description, source=NAME,
                                           options=options)
            if renew:
                await self.cbpi.config.add(NAME + "_update", self.version, type=ConfigType.STRING,
                                           description="Pushover Update Version", source="hidden")
        except Exception as e:  # noqa: BLE001
            logger.warning("Phone alerts: could not add their settings: %s", e)
        if self._keys() is None:
            logger.info("Phone alerts: off until %s and %s are both filled in (Settings).", USER, TOKEN)

    def _setting(self, name):
        """A setting as it is kept, read from the cache rather than with config.get(), which logs each read: the log
        filter reads the keys, and must not log while it does."""
        try:
            value = getattr(self.cbpi.config.cache.get(name), "value", None)
        except Exception:  # noqa: BLE001
            return ""
        return "" if value is None else str(value).strip()

    def _keys(self):
        """(user key, token), or None while either is blank: off."""
        user, token = self._setting(USER), self._setting(TOKEN)
        return (user, token) if user and token else None

    def _secrets(self):
        return [s for s in (self._setting(USER), self._setting(TOKEN)) if len(s) >= HIDE_FROM]

    def _on(self, kind):
        name = SWITCH[kind]
        return (self._setting(name) or self.BLANK[name]).lower() != "no"

    def _title(self):
        """The brewery's name, so the rig's alerts can't be taken for a simulator's."""
        return self._setting("BREWERY_NAME") or "CraftBeerPi"

    # -- what the screen beeps for --------------------------------------------------------------------------------

    async def _heard(self, cbpi, title, message, type, action):
        """A notification (NotificationController's listener, in a task of its own): its push, if the screen beeps
        for it and its kind's switch is on. Never raises."""
        try:
            kind = self._kind(title, message, type, action)
            if kind is None or not self._on(kind):
                return
            keys = self._keys()
            if keys is None:
                return
            if kind == "timers":
                await self._ran_out(_plain(title), keys)
                return
            if kind in ("errors", "warnings") and self._repeated(kind, _plain(title), _plain(message)):
                return
            await self._send(self._words(kind, title, message), HIGH, keys)
        except Exception as e:  # noqa: BLE001 - an alert that can't be sent never stops the brew
            logger.warning("Phone alerts: one wasn't sent: %s", self._hide(str(e)))

    @staticmethod
    def _kind(title, message, type, action):
        """Which of KINDS a notification is, or None: one the screen makes no sound for."""
        if action:
            if TIMER_RAN_OUT.fullmatch(_plain(message)):
                return "timers"
            if _plain(title) in ADDITIONS:
                return "additions"
            return "questions"
        return {"error": "errors", "warning": "warnings"}.get(getattr(type, "value", type))

    def _words(self, kind, title, message):
        """What the push says: short, plain, what to do."""
        title, message = _plain(title), _plain(message)
        if kind == "additions":
            return self._addition(message)
        if kind == "questions":
            return "CraftBeerPi asks: " + _join(title, message)
        if title.lower() in GENERIC:
            title = ""
        return ("Error: " if kind == "errors" else "Warning: ") + _join(title, message)

    def _addition(self, message):
        """"Boil: add 1.50 oz Citra now": the step the brew is on, then the addition."""
        for pattern, words in ADDITION_WORDS:
            found = pattern.fullmatch(message)
            if found:
                what = words.format(**found.groupdict())
                break
        else:
            what = message
        step = self._step_name()
        return f"{step}: {what}" if step else what[:1].upper() + what[1:]

    def _step_name(self):
        """The running step's name, or "" when none runs."""
        try:
            step = self.cbpi.step.find_by_status(StepState.ACTIVE)
        except Exception:  # noqa: BLE001
            return ""
        return _plain(getattr(step, "name", "")) if step is not None else ""

    def _repeated(self, *words):
        """Whether these words were sent less than REPEAT_WINDOW brewing seconds ago; noted as sent now if not."""
        now = clock.monotonic()
        while self._sent and now - next(iter(self._sent.values())) >= REPEAT_WINDOW:
            self._sent.popitem(last=False)
        if words in self._sent:
            return True
        self._sent[words] = now
        while len(self._sent) > REMEMBERED:
            self._sent.popitem(last=False)
        return False

    # -- the timers -------------------------------------------------------------------------------------------------

    def _timers(self):
        return getattr(getattr(self.cbpi, "timers", None), "timers", None) or []

    async def _ran_out(self, name, keys):
        """A timer has run out: its emergency push, unless this run-out has had one."""
        t = next((t for t in self._timers() if getattr(t, "name", None) == name), None)
        if t is None or getattr(t, "state", None) != "done" or getattr(t, "prompt", None) is None:
            return  # answered already
        alarm = self._alarms.get(t.id)
        if alarm is not None and (alarm.prompt == t.prompt or getattr(t, "interrupted", False)):
            # Its push was sent. Brought back after a restart, it rings again under a new prompt.
            self._adopt(alarm, t)
            return
        if alarm is not None:
            # It ran out again before the last alarm was seen to end: that one is over.
            await self._end(alarm)
        alarm = Alarm(t.id, t.name, t.prompt)
        self._alarms[t.id] = alarm
        self._save()
        self._watch()
        _, receipt, _ = await self._send(f"Timer {t.name.upper()}: time's up", EMERGENCY, keys,
                                         retry=RETRY, expire=EXPIRE)
        if receipt is None:
            return
        if self._alarms.get(t.id) is alarm:
            alarm.receipt = receipt
            self._save()
        else:
            # Answered while the push was on its way.
            await self._cancel(Alarm(t.id, t.name, t.prompt, receipt))

    def _adopt(self, alarm, t):
        if alarm.prompt != t.prompt:
            alarm.prompt = t.prompt
            self._save()

    @on_startup("Phone alerts")
    async def _restored(self):
        """Run by CraftBeerPi once the timers are back (TimersController.init comes before it): a push left repeating
        when CraftBeerPi stopped goes on for a timer brought back run out, and is cancelled for any other. Then the
        timers are watched."""
        self._ready = True
        await self._check()
        self._watch()

    def _watch(self):
        if self._ready and self._alarms and (self._watcher is None or self._watcher.done()):
            self._watcher = asyncio.get_running_loop().create_task(self._watching())

    async def _watching(self):
        """About once a real second while a timer's push repeats on the phone, as the timers themselves wait."""
        try:
            while self._alarms:
                await clock.sleep(max(1.0, clock.scale()))
                await self._check()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.warning("Phone alerts: stopped watching the timers: %s", self._hide(str(e)))

    async def _check(self):
        """Each timer whose push repeats: still run out, under the same prompt or brought back after a restart, it
        goes on; answered, set anew, removed or run out again since, its repeats are cancelled."""
        timers = {getattr(t, "id", None): t for t in self._timers()}
        for alarm in list(self._alarms.values()):
            t = timers.get(alarm.timer)
            if t is not None and getattr(t, "state", None) == "done":
                if t.prompt == alarm.prompt:
                    continue
                if getattr(t, "interrupted", False):
                    self._adopt(alarm, t)
                    continue
            await self._end(alarm)

    async def _end(self, alarm):
        """A timer's alarm is over: the phone stops too."""
        if self._alarms.get(alarm.timer) is not alarm:
            return
        del self._alarms[alarm.timer]
        self._save()
        if alarm.receipt is not None:
            await self._cancel(alarm)

    async def _cancel(self, alarm):
        keys = self._keys()
        if keys is None:
            logger.info("Phone alerts: the alarm for %s goes on on the phone until it's acknowledged there: the "
                        "token is blank, so it can't be stopped from here.", alarm.name)
            return
        answer, why = await self._post(f"{self.api}/receipts/{alarm.receipt}/cancel.json", dict(token=keys[1]))
        if answer is None:
            logger.warning("Phone alerts: the alarm for %s wasn't stopped on the phone: %s. Acknowledge it there.",
                           alarm.name, self._hide(why))
        else:
            logger.info("Phone alerts: the alarm for %s stopped on the phone: it was answered.", alarm.name)

    # -- kept through a restart -------------------------------------------------------------------------------------

    def _load(self):
        """The timers' pushes still repeating when CraftBeerPi stopped, from FILE."""
        if self._path is None:
            return
        try:
            with open(self._path, encoding="utf-8") as handle:
                data = json.load(handle)
        except FileNotFoundError:
            return
        except (OSError, ValueError) as e:
            logger.warning("Phone alerts: %s can't be read (%s), so a timer's alarm left repeating on the phone when "
                           "CraftBeerPi stopped won't be stopped from here.", FILE, e)
            return
        for item in (data.get("alarms") if isinstance(data, dict) else None) or []:
            if not isinstance(item, dict):
                continue
            timer, receipt = item.get("timer"), item.get("receipt")
            if isinstance(timer, bool) or not isinstance(timer, int):
                continue
            if not (isinstance(receipt, str) and RECEIPT.fullmatch(receipt)):
                receipt = None
            self._alarms[timer] = Alarm(timer, _plain(item.get("name")) or f"timer {timer}", item.get("prompt"),
                                        receipt)

    def _save(self):
        if self._path is None:
            return
        try:
            atomic_write_json(self._path, dict(version=FILE_VERSION, alarms=[a.to_dict() for a in
                                                                              self._alarms.values()]), sort_keys=True)
        except Exception as e:  # noqa: BLE001
            logger.warning("Phone alerts: could not save %s, so after a restart a timer's alarm may go on on the "
                           "phone after it is answered: %s", FILE, e)

    # -- Pushover ---------------------------------------------------------------------------------------------------

    def _hide(self, text):
        return _hide(str(text), self._secrets())

    def _client(self):
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=TIMEOUT))
        return self._session

    async def _post(self, url, data):
        """POST a form to Pushover, once. -> (its answer, None), or (None, why not)."""
        try:
            async with self._client().post(url, data=data) as response:
                try:
                    answer = await response.json(content_type=None)
                except ValueError:
                    answer = None
                if response.status == 200 and isinstance(answer, dict) and answer.get("status") == 1:
                    return answer, None
                errors = answer.get("errors") if isinstance(answer, dict) else None
                said = "; ".join(str(e) for e in errors) if isinstance(errors, list) and errors else ""
                return None, f"Pushover answered {response.status}" + (f": {said}" if said else "")
        except asyncio.TimeoutError:
            return None, f"Pushover didn't answer within {TIMEOUT} s"
        except aiohttp.ClientError as e:
            return None, f"Pushover couldn't be reached ({type(e).__name__}: {e})"

    async def _send(self, text, priority, keys, **extra):
        """One push: `text` under the brewery's name. -> (sent, Pushover's receipt or None, why not or None)."""
        user, token = keys
        data = dict(token=token, user=user, title=_cut(self._title(), TITLE_MAX), message=_cut(text, MESSAGE_MAX),
                    priority=priority, **extra)
        answer, why = await self._post(f"{self.api}/messages.json", data)
        if answer is None:
            why = self._hide(why)
            logger.warning("Phone alerts: \"%s\" wasn't sent: %s. It isn't tried again.", text, why)
            return False, None, why
        if priority == EMERGENCY:
            logger.info("Phone alerts: sent, repeating until it's acknowledged: %s", text)
        else:
            logger.info("Phone alerts: sent: %s", text)
        receipt = answer.get("receipt")
        return True, receipt if isinstance(receipt, str) and RECEIPT.fullmatch(receipt) else None, None

    @request_mapping(path="/test", method="POST", auth_required=False)
    async def http_test(self, request):
        """
        ---
        description: Send a test alert to the phone through Pushover, whatever each kind's switch says. 409 while the user key or the application token is blank. 502 if Pushover didn't take it, with why in error.
        tags:
        - Phone alerts
        responses:
            "200":
                description: sent
            "409":
                description: off, with the user key or the application token blank
            "502":
                description: Pushover didn't take it
        """
        keys = self._keys()
        if keys is None:
            return web.json_response(dict(sent=False, error=f"Fill in {USER} and {TOKEN} in Settings first."),
                                     status=409)
        sent, _, why = await self._send(TEST_TEXT, HIGH, keys)
        if not sent:
            return web.json_response(dict(sent=False, error=why), status=502)
        return web.json_response(dict(sent=True))

    async def _close(self, app=None):
        if self._watcher is not None and not self._watcher.done():
            self._watcher.cancel()
        if self._session is not None and not self._session.closed:
            await self._session.close()


def setup(cbpi):
    cbpi.plugin.register("PushOver", PushOver)
