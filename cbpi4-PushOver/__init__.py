# -*- coding: utf-8 -*-
"""Phone alerts through ntfy, or Pushover: one alert for each thing the brew-day screen beeps for.

The brew-day screen sounds its alarm for a timer that has run out, again and
again until the timer is answered, and gives two beeps for each new alert that
takes its title bar: an addition, a question, an error, a warning. Each of
those reaches CraftBeerPi as a notification (NotificationController.notify),
and this sends an alert for it from the server, so it reaches the phone with
no browser open.

It goes through the service chosen in phone_alerts_service:

- ntfy, the default: free, with no account and no payment. The Pi posts each
  alert to a topic on ntfy.sh (ntfy_server), and the free ntfy app, subscribed
  to the same topic, shows it. On ntfy.sh the topic is the password, so it
  should be long and random: GET /phone-alerts/topic gives one. ntfy can't
  repeat an alert until it is acknowledged, nor take back one that has reached
  an iPhone, so a run-out timer's alert is sent again every RESEND_EVERY
  brewing seconds, RESENDS more times at most, until the timer is answered on
  a screen. The ntfy iPhone app as released (1.7.0) has no critical or
  time-sensitive alerts: every alert arrives as an ordinary notification,
  heard only with the ringer on and no Focus.
- Pushover, the alternative: an account, and an app bought after a trial. A
  run-out timer's alert is an emergency alert, which Pushover repeats until it
  is acknowledged on the phone, 50 times at most, and which is cancelled once
  the timer is answered on a screen - OK, +5 MIN, set anew or removed -
  through Pushover's receipts API. Its app's Critical Alerts can sound on
  silent.

What is sent:

- a brew-day timer that has run out (TimersController: its state "done", its
  prompt titled with its name and worded "The 15:00 timer has run out."):
  ntfy's urgent priority (5), Pushover's emergency priority (2);
- the boil's addition reminders, Hop Alert and First Wort Hop Addition!
  (BoilStep, and the runoff's first-wort hops in StepController): high
  priority, ntfy's 4 and Pushover's 1;
- any other notification with buttons, a question waiting for an answer:
  high priority;
- an error or a warning with no buttons: high priority.

Info and success notifications make no sound on the screen and send nothing.

One alert for each notification, and never another for it: a page that
reloads or reconnects is shown the prompts still waiting as they were, with no
new notification. After a restart, a timer brought back run out raises its
prompt again; its alert was sent before, so it isn't sent again, and ntfy's
resends and Pushover's cancel carry on. Which timers' alerts are still under
way is kept in FILE, in the config folder, for that. The same error or warning
again within REPEAT_WINDOW brewing seconds isn't sent again either: the screen
shows the same words once.

Off until the chosen service is set up - ntfy's topic, or Pushover's user key
and application token: nothing is sent then, and nothing is logged as an
error. Each kind has its own switch, on by default. A test alert: POST
/phone-alerts/test, or /pushover/test as before, with kind "alert" or "timer";
its answer says, in a form a page can read, whether it was sent and if not
why (http_test).

For a bench, CBPI_NTFY_SERVER and CBPI_PUSHOVER_API in the environment point
each service at a fake.

Sending never holds the brew up: each alert goes out in its own task, through
aiohttp, which gives up after TIMEOUT seconds. An alert that fails is said once
in the log, as a warning, and isn't tried again. Without the internet the
alerts are lost: the screens and the buzzer remain the alarms.

The topic, the tokens and the user key never reach the log. What this writes
leaves them out, and a filter on the log's handlers replaces them wherever
else they would appear: CraftBeerPi writes every setting into the log at debug
level. ntfy's alerts are posted as JSON to the server's root, so the topic
isn't in any address either.
"""

import asyncio
import importlib.metadata
import json
import logging
import os
import re
from collections import OrderedDict
from secrets import choice as _random_choice

import aiohttp
from aiohttp import web
from cbpi.api import CBPiExtension, clock, on_startup, request_mapping
from cbpi.api.config import ConfigType
from cbpi.api.persist import atomic_write_json
from cbpi.api.step import StepState

logger = logging.getLogger(__name__)

NAME = "cbpi4-PushOver"

NTFY, PUSHOVER = "ntfy", "Pushover"

#: ntfy's server, unless CBPI_NTFY_SERVER names another.
NTFY_SERVER = "https://ntfy.sh"
NTFY_ENV = "CBPI_NTFY_SERVER"
#: Pushover's API, unless CBPI_PUSHOVER_API names another.
API = "https://api.pushover.net/1"
API_ENV = "CBPI_PUSHOVER_API"

#: Real seconds a request may take before it is given up. The network's time, not the brewing clock's: a simulation
#: doesn't make the internet faster.
TIMEOUT = 10

#: ntfy's priorities: 5 (urgent) for a run-out timer, 4 (high) for the rest. The ntfy iPhone app as released (1.7.0)
#: has no entitlement for time-sensitive or critical alerts, so every priority arrives there as an ordinary
#: notification. 5 costs nothing, and would sound on silent if a later app adds critical alerts. On Android both
#: vibrate long and pop up.
NTFY_URGENT, NTFY_HIGH = 5, 4
#: Each kind's tags: emoji short codes from ntfy's list, which the app shows before the title. 🚨 🌿 ❓ ⚠️ ❕, and ✅
#: for the test; a test of a timer's alert has the timer's.
NTFY_TAGS = {
    "timers": ["rotating_light"],
    "additions": ["herb"],
    "questions": ["question"],
    "errors": ["warning"],
    "warnings": ["grey_exclamation"],
    "test": ["white_check_mark"],
    "test_timer": ["rotating_light"],
}
#: A run-out timer's alert on ntfy goes again every RESEND_EVERY brewing seconds while the timer rings, RESENDS more
#: times at most: up to 11 alerts, over 20 minutes.
RESEND_EVERY, RESENDS = 120, 10
#: A topic's name, as ntfy takes one.
TOPIC = re.compile(r"[-_A-Za-z0-9]{1,64}")
#: A suggested topic: cbpi- and SUGGEST random letters and digits. Lower case, and without l, o, 0 and 1, which are
#: easily mistaken for one another when typed into a phone: 32 to choose from, so 120 random bits.
SUGGEST, SUGGEST_FROM = 24, "abcdefghijkmnpqrstuvwxyz23456789"

#: A run-out timer's alert on Pushover: emergency priority, repeated by Pushover every RETRY real seconds until it is
#: acknowledged on the phone, as the screen's alarm repeats until the timer is answered. Pushover stops at 50 repeats
#: whatever the expiry, so EXPIRE is 50 minutes' worth.
EMERGENCY, RETRY, EXPIRE = 2, 60, 3000
#: Everything else on Pushover: high priority, one alert.
HIGH = 1
#: A test of a timer's alert on Pushover: an emergency alert, cancelled TEST_CANCEL_AFTER real seconds after it is
#: sent, once Pushover has repeated it once. Should the cancel not reach Pushover, it stops by itself after
#: TEST_EXPIRE seconds.
TEST_CANCEL_AFTER, TEST_EXPIRE = RETRY + 15, 180

#: What a test's answer says went wrong (http_test), and the HTTP status for each.
STATUS = {"bad_request": 400, "not_set_up": 409, "limit": 429, "refused": 502, "no_devices": 502, "unreachable": 504}

#: Brewing seconds within which the same error or warning isn't sent again.
REPEAT_WINDOW = 600
#: The most of those remembered at once.
REMEMBERED = 500

#: The longest title and message sent: Pushover's limits, and well within ntfy's.
TITLE_MAX, MESSAGE_MAX = 250, 1024

#: In the config folder: the timers whose alerts are still under way, so a restart can still stop them.
FILE = "pushover.json"
FILE_VERSION = 1

S_SERVICE = "phone_alerts_service"
S_TOPIC, S_SERVER, S_NTFY_TOKEN, S_LINK = "ntfy_topic", "ntfy_server", "ntfy_token", "ntfy_link"
S_USER, S_TOKEN = "pushover_user", "pushover_token"
KINDS = ("timers", "additions", "questions", "errors", "warnings")
SWITCH = {kind: "phone_alerts_" + kind for kind in KINDS}
#: The switches' names in 0.0.10.post1, carried over to SWITCH and removed.
OLD_SWITCH = {kind: "pushover_" + kind for kind in KINDS}
YES_NO = [{"label": "Yes", "value": "Yes"}, {"label": "No", "value": "No"}]
SERVICES = [{"label": "ntfy", "value": NTFY}, {"label": "Pushover", "value": PUSHOVER}]

#: The settings: name, kind, choices, what it does. Each "(default: ...)" is what BLANK takes it as.
SETTINGS = (
    (S_SERVICE, ConfigType.SELECT, SERVICES,
     "Phone alerts: where they go. ntfy is free, with no account: the free ntfy app on your phone and a topic "
     "(ntfy_topic). On an iPhone its alerts sound only with the ringer on and no Focus. Pushover is the alternative: "
     "an account and an app bought after a 30-day trial (pushover_user and pushover_token); its timer alerts repeat "
     "until you acknowledge them, and can sound on silent. (default: ntfy)"),
    (S_TOPIC, ConfigType.STRING, None,
     "Phone alerts through ntfy: the topic your phone subscribes to in the ntfy app, the same in both places. On "
     "ntfy.sh the topic is the password: anyone who knows it can read your alerts and send you theirs. Use cbpi- and "
     "24 or more random letters and digits: open /phone-alerts/topic on this CraftBeerPi for one. Blank: no alerts."),
    (S_SERVER, ConfigType.STRING, None,
     "Phone alerts through ntfy: the server the Pi posts to. Leave it at https://ntfy.sh. "
     "(default: https://ntfy.sh)"),
    (S_NTFY_TOKEN, ConfigType.STRING, None,
     "Phone alerts through ntfy: an access token, starting tk_, only for a topic reserved with a paid ntfy.sh "
     "account. ntfy.sh needs no account, so this is normally blank. Blank: none."),
    (S_LINK, ConfigType.STRING, None,
     "Phone alerts through ntfy: a web address an alert opens when you tap it, such as the brew-day screen on your "
     "home network: http://<your Pi>:8000/. Blank: tapping opens the ntfy app."),
    (SWITCH["timers"], ConfigType.SELECT, YES_NO,
     "Phone alerts: a brew-day timer that has run out. Through ntfy: an urgent alert, sent again every 2 minutes, "
     "10 more times at most, until you answer the timer on a screen; on an iPhone each comes as an ordinary "
     "notification, heard only with the ringer on and no Focus, and alerts already there stay there. Through "
     "Pushover: an emergency alert, repeated every minute for up to 50 minutes until you acknowledge it on the "
     "phone, and stopped when you answer the timer on a screen. (default: Yes)"),
    (SWITCH["additions"], ConfigType.SELECT, YES_NO,
     "Phone alerts: a boil addition is due - the boil step's hop and first-wort reminders. One alert each. "
     "(default: Yes)"),
    (SWITCH["questions"], ConfigType.SELECT, YES_NO,
     "Phone alerts: CraftBeerPi asks something and waits for your answer on a screen. One alert each. "
     "(default: Yes)"),
    (SWITCH["errors"], ConfigType.SELECT, YES_NO,
     "Phone alerts: an error, such as a step that failed or an element left on by hand past its target. One alert "
     "each. (default: Yes)"),
    (SWITCH["warnings"], ConfigType.SELECT, YES_NO,
     "Phone alerts: a warning, which the brew-day screen beeps for too. One alert each. (default: Yes)"),
    (S_USER, ConfigType.STRING, None,
     "Phone alerts: not needed for ntfy. For Pushover, the alternative: your Pushover user key, 30 letters and "
     "digits, on your dashboard at pushover.net and in the Pushover app's settings. Used when phone_alerts_service is "
     "Pushover, with pushover_token. Blank: no alerts through Pushover."),
    (S_TOKEN, ConfigType.STRING, None,
     "Phone alerts: not needed for ntfy. For Pushover, the alternative: the API token, 30 letters and digits, of the "
     "application you created for CraftBeerPi at pushover.net/apps/build. Used when phone_alerts_service is "
     "Pushover, with pushover_user. Blank: no alerts through Pushover."),
)

#: How TimersController words a timer's alarm, as the brew-day screen knows it (TIMER_RAN_OUT there).
TIMER_RAN_OUT = re.compile(r"The \d+(?::\d\d){1,2} timer has run out\.")
#: The titles of the boil's addition reminders: BoilStep's hop alerts and first-wort hops, and the runoff's
#: first-wort hops (StepController._first_wort_at_runoff).
ADDITIONS = ("Hop Alert", "First Wort Hop Addition!")
#: How those reminders are worded, and what the alert says instead: what to do, and when.
ADDITION_WORDS = (
    (re.compile(r"Please add (?P<what>.+) \(\d+\)"), "add {what} now"),  # a hop alert: "Please add 1.50 oz Citra (1)"
    (re.compile(r"Please add Hop (?P<n>\d+)"), "add hop {n} now"),  # a hop alert with no text
    (re.compile(r"Please add (?P<what>.+) for first wort"), "add {what} for first wort now"),  # at the boil's start
    (re.compile(r"Please add (?P<what>.+)"), "add {what}"),  # the runoff's: "... before the first runnings reach it"
)
#: Titles that say nothing but the level: NOTIFY_ON_ERROR's, from the log, and "Error".
GENERIC = ("error", "warning", "critical")

#: A receipt as Pushover gives one, before it goes into an address.
RECEIPT = re.compile(r"[A-Za-z0-9]{1,64}")
#: A web address an ntfy alert can open.
LINK = re.compile(r"https?://\S+")
#: The shortest secret the log filter looks for: shorter, hiding it would mangle the log's lines, and a topic that
#: short is guessed anyway.
HIDE_FROM = 4
HIDDEN = "[hidden]"


def _version():
    try:
        return importlib.metadata.version(NAME)
    except importlib.metadata.PackageNotFoundError:
        return "0.0.0"


def suggest_topic():
    """A new topic for ntfy: cbpi- and SUGGEST random letters and digits."""
    return "cbpi-" + "".join(_random_choice(SUGGEST_FROM) for _ in range(SUGGEST))


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
    for secret in sorted(secrets, key=len, reverse=True):
        text = text.replace(secret, HIDDEN)
    return text


def _failure(reason, detail, field=None):
    """Why an alert wasn't sent, as a test's answer gives it: reason, one of STATUS; the setting at fault, "user",
    "token" or "topic" (the chosen service's), or None; and what happened, in words."""
    return dict(reason=reason, field=field, detail=detail)


class Alarm:
    """A run-out timer's alert while it is under way: the timer's id and name, the prompt that rang it, the service it
    went to; on Pushover its receipt (None until Pushover answers, or if it didn't); on ntfy how many have been sent
    and when the next is due (clock.monotonic(), None when no more are)."""

    __slots__ = ("timer", "name", "prompt", "service", "receipt", "sent", "due")

    def __init__(self, timer, name, prompt, service, receipt=None, sent=1, due=None):
        self.timer, self.name, self.prompt, self.service = timer, name, prompt, service
        self.receipt, self.sent, self.due = receipt, sent, due

    def to_dict(self):
        return dict(timer=self.timer, name=self.name, prompt=self.prompt, service=self.service, receipt=self.receipt,
                    sent=self.sent)


class _HideKeys(logging.Filter):
    """Replaces the ntfy topic, the tokens and the Pushover user key in each line before a handler writes it, wherever
    the line comes from: CraftBeerPi logs every setting at debug level (the websocket's broadcast lines)."""

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

    #: What each setting left blank is taken as (blank_defaults): ntfy, on ntfy.sh, and each kind's switch on. The
    #: topic, the tokens, the user key and the link aren't here: blank, they mean off, or none, which isn't a value to
    #: fill in.
    BLANK = {S_SERVICE: NTFY, S_SERVER: NTFY_SERVER, **{SWITCH[kind]: "Yes" for kind in KINDS}}

    @classmethod
    def blank_defaults(cls, unit=None):
        """What each setting left blank is taken as: BLANK, which the plugin reads as well, so the two can't drift
        apart. None depends on the temperature unit."""
        return dict(cls.BLANK)

    def __init__(self, cbpi):
        self.cbpi = cbpi
        self.version = _version()
        self.api = (os.environ.get(API_ENV) or "").strip().rstrip("/") or API
        self.ntfy_server = (os.environ.get(NTFY_ENV) or "").strip().rstrip("/")
        self._session = None
        # The errors and warnings sent lately, oldest first: (kind, title, message) -> clock.monotonic() then.
        self._sent = OrderedDict()
        # Timer id -> its Alarm, while its alert is under way.
        self._alarms = {}
        self._watcher = None
        # True once the timers are back after a start (_restored): only then does a missing timer mean its alarm is
        # over.
        self._ready = False
        # Mistakes in the settings already said in the log, each once.
        self._said = set()
        # Tests of a timer's alert on Pushover waiting to be cancelled.
        self._tests = set()
        folder = getattr(cbpi, "config_folder", None)
        self._path = folder.get_file_path(FILE) if folder is not None else None
        self._load()
        self._hiding = _HideKeys.install(self)
        # Heard from the start, before the steps and the timers are brought back: "Resume brew?" and a timer brought
        # back run out are raised as CraftBeerPi starts.
        cbpi.notification.add_listener(self._heard)
        cbpi.register(self, url_prefix="/pushover")
        # The same endpoints under a name that fits either service.
        cbpi.register_http_endpoints(self, url_prefix="/phone-alerts")
        app = getattr(cbpi, "app", None)
        if app is not None:
            app.on_cleanup.append(self._close)
        self._declaring = asyncio.get_running_loop().create_task(self._declare())

    # -- the settings ---------------------------------------------------------------------------------------------

    async def _declare(self):
        """The settings: added when missing, and brought up to date - descriptions, choices - with each new version.
        A setting with a value it is taken as (BLANK) is given that value when it is blank, or set to what it doesn't
        offer, so Settings shows what is in force. The switches of 0.0.10.post1, pushover_timers and the like, are
        carried over to their new names. An install already sending through Pushover stays on Pushover; any other
        starts on ntfy. Never raises: without its settings the plugin is off."""
        config = self.cbpi.config
        try:
            renew = config.get(NAME + "_update", None) != self.version
            carried = {SWITCH[kind]: config.cache[OLD_SWITCH[kind]].value for kind in KINDS
                       if OLD_SWITCH[kind] in config.cache and SWITCH[kind] not in config.cache}
            if S_SERVICE not in config.cache:
                carried[S_SERVICE] = PUSHOVER if self._pushover() else NTFY
            for name, kind, options, description in SETTINGS:
                entry = config.cache.get(name)
                if entry is not None and not renew:
                    continue
                value = carried.get(name, "") if entry is None or entry.value is None else entry.value
                if name in self.BLANK and (value == "" or (options and value not in [o["value"] for o in options])):
                    value = self.BLANK[name]
                await config.add(name, value, type=kind, description=description, source=NAME, options=options)
            for kind in KINDS:
                if OLD_SWITCH[kind] in config.cache:
                    await config.remove(OLD_SWITCH[kind])
            if renew:
                await config.add(NAME + "_update", self.version, type=ConfigType.STRING,
                                 description="Pushover Update Version", source="hidden")
        except Exception as e:  # noqa: BLE001
            logger.warning("Phone alerts: could not add their settings: %s", e)
        if self._off():
            logger.info("Phone alerts: off until %s is filled in (Settings).",
                        f"{S_USER} and {S_TOKEN}" if self._service() == PUSHOVER else S_TOPIC)

    def _setting(self, name):
        """A setting as it is kept, read from the cache rather than with config.get(), which logs each read: the log
        filter reads the secrets, and must not log while it does."""
        try:
            value = getattr(self.cbpi.config.cache.get(name), "value", None)
        except Exception:  # noqa: BLE001
            return ""
        return "" if value is None else str(value).strip()

    def _service(self):
        """The service chosen: ntfy, unless phone_alerts_service says Pushover."""
        return PUSHOVER if self._setting(S_SERVICE).lower() == PUSHOVER.lower() else NTFY

    def _pushover(self):
        """(user key, token), or None while either is blank."""
        user, token = self._setting(S_USER), self._setting(S_TOKEN)
        return (user, token) if user and token else None

    def _ntfy(self):
        """Where ntfy's alerts go - (server, topic, token, link) - or (None, why not, as _failure gives it)."""
        topic = self._setting(S_TOPIC)
        if not topic:
            return None, _failure("not_set_up", f"Fill in {S_TOPIC} in Settings first: the same topic as in the ntfy "
                                                "app", "topic")
        if not TOPIC.fullmatch(topic):
            return None, _failure("not_set_up", f"{S_TOPIC} can hold only letters, digits, - and _, 64 at most",
                                  "topic")
        server = (self.ntfy_server or self._setting(S_SERVER) or NTFY_SERVER).rstrip("/")
        if not LINK.fullmatch(server):
            return None, _failure("not_set_up", f"{S_SERVER} isn't a web address: leave it at {NTFY_SERVER}")
        link = self._setting(S_LINK)
        if link and not LINK.fullmatch(link):
            self._say_once(S_LINK, f"Phone alerts: {S_LINK} isn't a web address (it starts http:// or https://), so "
                                   "tapping an alert opens the ntfy app.")
            link = ""
        return (server, topic, self._setting(S_NTFY_TOKEN), link), None

    def _off(self):
        """True while the chosen service has nothing to send to: ntfy without a topic, Pushover without both keys."""
        if self._service() == PUSHOVER:
            return self._pushover() is None
        return not self._setting(S_TOPIC)

    def _not_set_up(self):
        """Why the chosen service can't send - blank or wrong settings - as _failure gives it, or None."""
        return self._pushover_missing() if self._service() == PUSHOVER else self._ntfy()[1]

    def _pushover_missing(self):
        """Why Pushover can't send - a key left blank - as _failure gives it, or None."""
        for field, name in (("user", S_USER), ("token", S_TOKEN)):
            if not self._setting(name):
                return _failure("not_set_up", f"Fill in {S_USER} and {S_TOKEN} in Settings first", field)
        return None

    def _secrets(self):
        return [s for s in (self._setting(S_TOPIC), self._setting(S_NTFY_TOKEN), self._setting(S_USER),
                            self._setting(S_TOKEN)) if len(s) >= HIDE_FROM]

    def _on(self, kind):
        name = SWITCH[kind]
        return (self._setting(name) or self.BLANK[name]).lower() != "no"

    def _title(self):
        """The brewery's name, so the rig's alerts can't be taken for a simulator's."""
        return self._setting("BREWERY_NAME") or "CraftBeerPi"

    def _say_once(self, key, words):
        if (key, words) not in self._said:
            self._said.add((key, words))
            logger.warning(words)

    # -- what the screen beeps for --------------------------------------------------------------------------------

    async def _heard(self, cbpi, title, message, type, action):
        """A notification (NotificationController's listener, in a task of its own): its alert, if the screen beeps
        for it, its kind's switch is on and the chosen service is set up. Never raises."""
        try:
            kind = self._kind(title, message, type, action)
            if kind is None or not self._on(kind) or self._off():
                return
            if kind == "timers":
                await self._ran_out(_plain(title))
                return
            if kind in ("errors", "warnings") and self._repeated(kind, _plain(title), _plain(message)):
                return
            await self._send(kind, self._words(kind, title, message))
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
        """What the alert says: short, plain, what to do."""
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

    def _timer(self, ident):
        return next((t for t in self._timers() if getattr(t, "id", None) == ident), None)

    async def _ran_out(self, name):
        """A timer has run out: its alert, unless this run-out has had one."""
        t = next((t for t in self._timers() if getattr(t, "name", None) == name), None)
        if t is None or getattr(t, "state", None) != "done" or getattr(t, "prompt", None) is None:
            return  # answered already
        alarm = self._alarms.get(t.id)
        if alarm is not None and (alarm.prompt == t.prompt or getattr(t, "interrupted", False)):
            # Its alert was sent. Brought back after a restart, it rings again under a new prompt.
            self._adopt(alarm, t)
            return
        if alarm is not None:
            # It ran out again before the last alarm was seen to end: that one is over.
            await self._end(alarm)
        service = self._service()
        alarm = Alarm(t.id, t.name, t.prompt, service,
                      due=clock.monotonic() + RESEND_EVERY if service == NTFY else None)
        self._alarms[t.id] = alarm
        self._save()
        self._watch()
        _, receipt, _ = await self._send("timers", f"Timer {t.name.upper()}: time's up")
        if receipt is None:
            return
        if self._alarms.get(t.id) is alarm:
            alarm.receipt = receipt
            self._save()
        else:
            # Answered while the alert was on its way.
            await self._cancel(receipt, f"the alarm for {t.name}", "it was answered")

    def _adopt(self, alarm, t):
        if alarm.prompt != t.prompt:
            alarm.prompt = t.prompt
            self._save()

    @on_startup("Phone alerts")
    async def _restored(self):
        """Run by CraftBeerPi once the timers are back (TimersController.init comes before it): an alert under way
        when CraftBeerPi stopped goes on for a timer brought back run out - on ntfy, its next resend RESEND_EVERY
        brewing seconds from now - and is stopped for any other. Then the timers are watched."""
        self._ready = True
        await self._check()
        for alarm in self._alarms.values():
            if alarm.service == NTFY and alarm.due is None and alarm.sent <= RESENDS:
                alarm.due = clock.monotonic() + RESEND_EVERY
        self._watch()

    def _watch(self):
        if self._ready and self._alarms and (self._watcher is None or self._watcher.done()):
            self._watcher = asyncio.get_running_loop().create_task(self._watching())

    async def _watching(self):
        """While a timer's alert is under way: about once a real second, as the timers themselves wait, and as each of
        ntfy's resends falls due on the brewing clock."""
        try:
            while self._alarms:
                now = clock.monotonic()
                wait = min([max(1.0, clock.scale())] + [a.due - now for a in self._alarms.values() if a.due is not None])
                # A wait worked out from monotonic(), as the resends are due: clock.sleep_until() waits for a time on
                # clock.now(), which a monotonic() deadline is long past, so it wouldn't wait at all.
                await clock.sleep(max(0.0, wait))
                await self._check()
                await self._resend()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.warning("Phone alerts: stopped watching the timers: %s", self._hide(str(e)))

    async def _check(self):
        """Each timer whose alert is under way: still run out, under the same prompt or brought back after a restart,
        it goes on; answered, set anew, removed or run out again since, its alert stops."""
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

    async def _resend(self):
        """ntfy's alert for a timer still ringing, again, each time one falls due: RESENDS more at most. One that falls
        due while ntfy isn't the service chosen, isn't set up, or the timers' switch is off, isn't sent, and counts."""
        now = clock.monotonic()
        for alarm in list(self._alarms.values()):
            if alarm.service != NTFY or alarm.due is None or now < alarm.due:
                continue
            t = self._timer(alarm.timer)
            if self._alarms.get(alarm.timer) is not alarm or t is None or getattr(t, "state", None) != "done":
                continue  # answered: _check stops it
            alarm.sent += 1
            alarm.due = now + RESEND_EVERY if alarm.sent <= RESENDS else None
            self._save()
            if self._service() == NTFY and not self._off() and self._on("timers"):
                await self._send("timers", f"Timer {t.name.upper()}: time's up, still unanswered")

    async def _end(self, alarm):
        """A timer's alarm is over: on Pushover the phone stops too; on ntfy no more is sent."""
        if self._alarms.get(alarm.timer) is not alarm:
            return
        del self._alarms[alarm.timer]
        self._save()
        if alarm.service == PUSHOVER and alarm.receipt is not None:
            await self._cancel(alarm.receipt, f"the alarm for {alarm.name}", "it was answered")
        elif alarm.service == NTFY and alarm.sent <= RESENDS:
            logger.info("Phone alerts: no more alerts for %s: it was answered.", alarm.name)

    async def _cancel(self, receipt, what, why):
        """Pushover stops repeating an emergency alert, through its receipt. `what` it was and `why` it stops are for
        the log."""
        token = self._setting(S_TOKEN)
        if not token:
            logger.info("Phone alerts: %s goes on on the phone until it's acknowledged there: %s is blank, so it can't "
                        "be stopped from here.", what, S_TOKEN)
            return
        status, answer = await self._post(PUSHOVER, f"{self.api}/receipts/{receipt}/cancel.json",
                                          data=dict(token=token))
        failure = answer if status is None else self._pushover_failure(status, answer)
        if failure is not None:
            logger.warning("Phone alerts: %s wasn't stopped on the phone: %s. Acknowledge it there.", what,
                           self._hide(failure["detail"]))
        else:
            logger.info("Phone alerts: %s stopped on the phone: %s.", what, why)

    # -- kept through a restart -------------------------------------------------------------------------------------

    def _load(self):
        """The timers' alerts under way when CraftBeerPi stopped, from FILE."""
        if self._path is None:
            return
        try:
            with open(self._path, encoding="utf-8") as handle:
                data = json.load(handle)
        except FileNotFoundError:
            return
        except (OSError, ValueError) as e:
            logger.warning("Phone alerts: %s can't be read (%s), so a timer's alarm left under way when CraftBeerPi "
                           "stopped won't be stopped from here.", FILE, e)
            return
        for item in (data.get("alarms") if isinstance(data, dict) else None) or []:
            if not isinstance(item, dict):
                continue
            timer, receipt, sent = item.get("timer"), item.get("receipt"), item.get("sent", 1)
            if isinstance(timer, bool) or not isinstance(timer, int):
                continue
            if not (isinstance(receipt, str) and RECEIPT.fullmatch(receipt)):
                receipt = None
            if isinstance(sent, bool) or not isinstance(sent, int) or sent < 1:
                sent = 1
            service = NTFY if item.get("service") == NTFY else PUSHOVER
            self._alarms[timer] = Alarm(timer, _plain(item.get("name")) or f"timer {timer}", item.get("prompt"),
                                        service, receipt, sent)

    def _save(self):
        if self._path is None:
            return
        try:
            atomic_write_json(self._path, dict(version=FILE_VERSION, alarms=[a.to_dict() for a in
                                                                              self._alarms.values()]), sort_keys=True)
        except Exception as e:  # noqa: BLE001
            logger.warning("Phone alerts: could not save %s, so after a restart a timer's alarm may go on after it is "
                           "answered: %s", FILE, e)

    # -- the services -----------------------------------------------------------------------------------------------

    def _hide(self, text):
        return _hide(str(text), self._secrets())

    def _client(self):
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=TIMEOUT))
        return self._session

    async def _post(self, service, url, **body):
        """POST to a service, once. -> (HTTP status, its answer: a dict, {} if it isn't JSON), or (None, why it
        couldn't be reached, as _failure gives it)."""
        try:
            async with self._client().post(url, **body) as response:
                try:
                    answer = await response.json(content_type=None)
                except ValueError:
                    answer = None
                return response.status, answer if isinstance(answer, dict) else {}
        except asyncio.TimeoutError:
            return None, _failure("unreachable", f"{service} didn't answer within {TIMEOUT} s")
        except aiohttp.ClientError as e:
            return None, _failure("unreachable", f"{service} couldn't be reached ({type(e).__name__}: {e})")

    @staticmethod
    def _pushover_failure(status, answer):
        """Why Pushover didn't take a request, as _failure gives it, or None if it did. Its 400s name the key it
        refused ("user": "invalid", "token": "invalid"). Taken with "no active devices to send to", it has no phone
        to send to, which is not delivered either."""
        if status == 200 and answer.get("status") == 1:
            info = _plain(answer.get("info"))
            if "no active devices" not in info.lower():
                return None
            return _failure("no_devices", f"Pushover has no phone to send it to ({info}): sign in to the Pushover app "
                                          f"on the phone with the account whose user key is in {S_USER}", "user")
        errors = answer.get("errors")
        said = "; ".join(_plain(e) for e in errors) if isinstance(errors, list) else ""
        detail = f"Pushover answered {status}" + (f": {said}" if said else "")
        if status == 429:
            return _failure("limit", detail)
        if status >= 500:
            return _failure("unreachable", detail)
        field = "token" if answer.get("token") == "invalid" else "user" if answer.get("user") == "invalid" else None
        return _failure("refused", detail, field)

    @staticmethod
    def _ntfy_failure(status, answer, token):
        """Why ntfy didn't take a message, as _failure gives it, or None if it did. ntfy answers 401 for an access
        token it doesn't know, 403 for a topic reserved by someone else or closed to the token, 429 at a limit
        (42901 too many at once, 42908 the day's quota)."""
        if 200 <= status < 300:
            return None
        code, said = answer.get("code"), _plain(answer.get("error"))
        detail = f"ntfy answered {status}" + (f" ({code})" if code else "") + (f": {said}" if said else "")
        if status == 429:
            return _failure("limit", detail)
        if status >= 500:
            return _failure("unreachable", detail)
        field = {401: "token" if token else "topic", 403: "topic"}.get(status)
        return _failure("refused", detail, field)

    async def _send(self, kind, text):
        """One alert of `kind`, saying `text` under the brewery's name, on the chosen service. -> (sent, Pushover's
        receipt or None, why not as _failure gives it or None)."""
        service = self._service()
        if service == PUSHOVER:
            receipt, why = await self._send_pushover(kind, text)
        else:
            receipt, why = await self._send_ntfy(kind, text)
        if why is not None:
            why = dict(why, detail=self._hide(why["detail"]))
            logger.warning("Phone alerts: \"%s\" wasn't sent through %s: %s.", text, service, why["detail"])
            return False, None, why
        if service == PUSHOVER and kind == "timers":
            logger.info("Phone alerts: sent through Pushover, repeating until it's acknowledged: %s", text)
        else:
            logger.info("Phone alerts: sent through %s: %s", service, text)
        return True, receipt, None

    async def _send_ntfy(self, kind, text):
        """-> (None, None) once ntfy has it, or (None, why not)."""
        target, why = self._ntfy()
        if target is None:
            return None, why
        server, topic, token, link = target
        message = dict(topic=topic, title=_cut(self._title(), TITLE_MAX), message=_cut(text, MESSAGE_MAX),
                       priority=NTFY_URGENT if kind in ("timers", "test_timer") else NTFY_HIGH,
                       tags=list(NTFY_TAGS[kind]))
        if link:
            message["click"] = link
        # As JSON to the server's root, so the topic is in the body, not in an address an error could quote.
        status, answer = await self._post(NTFY, server + "/", json=message,
                                          headers={"Authorization": f"Bearer {token}"} if token else None)
        return None, answer if status is None else self._ntfy_failure(status, answer, token)

    async def _send_pushover(self, kind, text):
        """-> (its receipt or None, None) once Pushover has it, or (None, why not)."""
        keys = self._pushover()
        if keys is None:
            return None, self._pushover_missing()
        user, token = keys
        urgent = kind in ("timers", "test_timer")
        data = dict(token=token, user=user, title=_cut(self._title(), TITLE_MAX), message=_cut(text, MESSAGE_MAX),
                    priority=EMERGENCY if urgent else HIGH)
        if urgent:
            data.update(retry=RETRY, expire=EXPIRE if kind == "timers" else TEST_EXPIRE)
        status, answer = await self._post(PUSHOVER, f"{self.api}/messages.json", data=data)
        why = answer if status is None else self._pushover_failure(status, answer)
        if why is not None:
            return None, why
        receipt = answer.get("receipt")
        return receipt if isinstance(receipt, str) and RECEIPT.fullmatch(receipt) else None, None

    # -- the test and the topic -------------------------------------------------------------------------------------

    @request_mapping(path="/test", method="POST", auth_required=False)
    async def http_test(self, request):
        """
        ---
        description: "Send a test alert to the phone through the chosen service, ntfy or Pushover, whatever the kinds' switches say. kind, in the query or a JSON body: alert, the default, is sent as an addition's, question's or error's alert is - ntfy priority 4, Pushover high priority; timer as a run-out timer's is - ntfy priority 5 with its tag, sent once, or a Pushover emergency alert, cancelled once Pushover has repeated it once. The answer: sent; reason, null when sent, else not_set_up, refused, unreachable, limit, no_devices, or bad_request for a kind it doesn't know; field, the setting at fault - user, token or topic - or null; detail, in words; service and kind."
        tags:
        - Phone alerts
        parameters:
        - in: query
          name: kind
          required: false
          type: string
          enum: [alert, timer]
          default: alert
        responses:
            "200":
                description: sent
            "400":
                description: "bad_request: kind is neither alert nor timer"
            "409":
                description: "not_set_up: the chosen service's settings are blank or wrong; field names the one"
            "429":
                description: "limit: the service has had too many alerts lately"
            "502":
                description: "refused: the service didn't take it, field naming the setting it refused if it said; or no_devices: Pushover took it but has no phone signed in to send it to"
            "504":
                description: "unreachable: the service couldn't be reached, didn't answer in time, or failed itself"
        """
        service = self._service()
        kind = await self._test_kind(request)
        if kind not in ("alert", "timer"):
            return self._test_answer(service, kind, _failure("bad_request", "kind is alert or timer"))
        why = self._not_set_up()
        if why is not None:
            return self._test_answer(service, kind, why)
        if kind == "timer":
            words = (f"Test timer alert from CraftBeerPi through {service}: a timer that has run out " +
                     ("comes like this." if service == NTFY else
                      "comes like this, repeated until acknowledged. This one repeats once, then stops."))
        else:
            words = f"Test alert from CraftBeerPi through {service}: phone alerts work."
        sent, receipt, why = await self._send("test_timer" if kind == "timer" else "test", words)
        if not sent:
            return self._test_answer(service, kind, why)
        if kind == "timer" and service == PUSHOVER:
            if receipt is not None:
                self._cancel_test(receipt)
            detail = (f"Sent through Pushover as an emergency alert, as a run-out timer's is: Pushover repeats it "
                      f"once, about {RETRY} seconds from now, and then it stops.")
        elif kind == "timer":
            detail = (f"Sent through ntfy at priority {NTFY_URGENT}, as a run-out timer's alert is. CraftBeerPi sends "
                      f"a timer's alert again every {RESEND_EVERY // 60} minutes until it's answered; this test comes "
                      "once.")
        else:
            detail = f"Sent through {service}: it should be on the phone in a few seconds."
        return self._test_answer(service, kind, None, detail)

    @staticmethod
    async def _test_kind(request):
        """The kind of test asked for, from ?kind= or a JSON body's kind: "alert" when neither says."""
        kind = None
        if request is not None:
            kind = request.query.get("kind")
            if kind is None and request.can_read_body:
                try:
                    body = await request.json()
                except ValueError:
                    body = None
                if isinstance(body, dict):
                    kind = body.get("kind")
        return "alert" if kind is None or kind == "" else _plain(kind).lower()

    @staticmethod
    def _test_answer(service, kind, why, detail=None):
        """A test's answer, which a page can read: sent, reason, field, detail, service, kind. HTTP status: 200 when
        sent, else STATUS's for the reason."""
        if why is None:
            body = dict(sent=True, reason=None, field=None, detail=detail)
        else:
            body = dict(sent=False, reason=why["reason"], field=why["field"], detail=why["detail"])
        body.update(service=service, kind=kind)
        return web.json_response(body, status=200 if why is None else STATUS[why["reason"]])

    def _cancel_test(self, receipt):
        """A test of a timer's alert on Pushover stops once Pushover has repeated it once."""
        async def cancel():
            # Real seconds, not the brewing clock's: Pushover repeats the alert in real time.
            await asyncio.sleep(TEST_CANCEL_AFTER)
            await self._cancel(receipt, "the test timer alert", "the test is over")

        task = asyncio.get_running_loop().create_task(cancel())
        self._tests.add(task)
        task.add_done_callback(self._tests.discard)

    @request_mapping(path="/topic", method="GET", auth_required=False)
    async def http_topic(self, request):
        """
        ---
        description: A new random topic for ntfy, for ntfy_topic and the ntfy app - cbpi- and 24 random letters and digits. Not kept, and never logged.
        tags:
        - Phone alerts
        responses:
            "200":
                description: the topic, in topic
        """
        return web.json_response(dict(topic=suggest_topic()), headers={"Cache-Control": "no-store"})

    async def _close(self, app=None):
        if self._watcher is not None and not self._watcher.done():
            self._watcher.cancel()
        for task in list(self._tests):
            task.cancel()
        if self._session is not None and not self._session.closed:
            await self._session.close()


def setup(cbpi):
    cbpi.plugin.register("PushOver", PushOver)
