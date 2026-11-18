# Craftbeerpi4 PushOver Notifications Plugin

## Phone alerts, through ntfy or Pushover, for everything the brew-day screen beeps for

CraftBeerPi sends the alerts itself, from the Raspberry Pi, so they reach your phone with no browser open. They go
through **ntfy**, which is free, or through Pushover, the alternative.

### Software installation:

Please have a look at the [Craftbeerpi4 Documentation](https://openbrewing.gitbook.io/craftbeerpi4_support/readme/plugin-installation)

- Package name: cbpi4-PushOver
- Package link: https://github.com/PiBrewing/cbpi4-PushOver/archive/main.zip

This version needs a CraftBeerPi with the brew-day timers and the brewing clock (`cbpi.api.clock`).

### What it needs

The Pi needs the internet. Nothing else: no extra hardware, no account and no payment. The ntfy app is free, and so is
[ntfy.sh](https://ntfy.sh), the server the Pi posts the alerts to. ntfy.sh takes up to 250 alerts in any 24 hours from
one home internet address, shared by everything behind it, and 60 at once; a brew day sends far fewer.

### Turning it on with ntfy

Nothing is sent until the topic is filled in.

1. Install the free **ntfy** app on your iPhone, from the App Store.
2. Subscribe to a topic on ntfy.sh. Get one first: open `http://<your Pi>:8000/phone-alerts/topic` in a browser. It
   shows a new random one, such as `cbpi-k3mz8x2qwe7rtb4ny6hd9fva`, a different one each time. In the ntfy app, tap
   **+**, type the topic into "Topic name", leave "Use another server" off, and tap **Subscribe**.
3. Allow the app's notifications: it asks when it first opens. If you said no, turn them on for ntfy in the iPhone's
   Settings app.
4. In CraftBeerPi, on the Settings page, among the plugin settings for cbpi4-PushOver: type the same topic into
   `ntfy_topic`, and save. No restart is needed.
5. Send a test alert, and a test of a timer's alert:

       curl -X POST http://<your Pi>:8000/phone-alerts/test
       curl -X POST "http://<your Pi>:8000/phone-alerts/test?kind=timer"

**The topic is the password.** ntfy.sh has no accounts: anyone who knows your topic can read your alerts and send
you theirs. Keep it long and random, as the suggestions are, and don't share it.

**On brew day, keep the iPhone's ringer on, and no Focus.** ntfy's alerts come to an iPhone as ordinary
notifications. With the ring/silent switch on silent, an alert only vibrates, and only if the iPhone plays haptics in
silent mode. A Focus or Do Not Disturb holds it back.

### What is sent

One alert for each of these, as it happens. The title of each is your brewery's name (the BREWERY_NAME setting), so
the rig's alerts can't be taken for a simulator's.

| The brew-day screen | The alert | Through ntfy | Through Pushover |
|---|---|---|---|
| A timer that has run out: its alarm sounds again and again until it's answered | `Timer HOP STAND: time's up` | Urgent, priority 5, 🚨; sent again every 2 minutes, 10 more times at most, until the timer is answered | Emergency, priority 2: repeated every minute for up to 50 minutes until acknowledged on the phone, cancelled when the timer is answered |
| An addition: two beeps. The boil step's Hop Alert (Hop_1 to Hop_6) and its first-wort hops, and the first-wort hops asked for as the runoff starts | `Boil: add 1.50 oz Citra now` | Priority 4, 🌿 | High, priority 1 |
| A question: two beeps. Any other notification with a button to answer it | `CraftBeerPi asks: Resume brew? ...` | Priority 4, ❓ | High, priority 1 |
| An error: two beeps. A step that failed, an element on by hand past its target, ... | `Error: Brew Steps: Step 'Mash' failed and the profile has stopped. ...` | Priority 4, ⚠️ | High, priority 1 |
| A warning: two beeps | `Warning: ...` | Priority 4, ❕ | High, priority 1 |

Nothing else is sent: the screen makes no sound for information and success messages. An addition starts with the
name of the step running, `Boil` above.

The ntfy app for iPhone as released (1.7.0) shows every priority the same way, as an ordinary notification. A
timer's priority 5 costs nothing, and would sound on silent if a later version of the app adds critical alerts, which
ntfy lists for an unreleased 1.8.0. On Android, priorities 4 and 5 vibrate longer and pop up on the screen.

### What ntfy can't do on an iPhone

- **Sound with the phone on silent, or through a Focus or Do Not Disturb.** Keep the ringer on and no Focus on brew
  day.
- **Alerts that have arrived stay on the phone** after the timer or question is answered on a screen. ntfy can't take
  them back.
- **Nothing repeats until you acknowledge it on the phone.** A timer that has run out is sent again every 2 minutes,
  10 more times at most, and those resends stop once the timer is answered on any screen. That's all the repeating
  there is.

### Sent once

- Reloading a page, or a page reconnecting, sends nothing: the page is shown what still waits, with no new alert.
- After a restart, a timer brought back run out isn't sent again. ntfy's resends for it carry on, and on Pushover its
  alert still stops when the timer is answered. Which timers' alerts are under way is kept in `pushover.json` in the
  config folder for that.
- The same error or warning again within 10 minutes isn't sent again, as the screen shows the same words once.
- A question CraftBeerPi asks again after a restart (Resume brew?, add the malt, ...) is a new question, and a new
  alert.

### The settings

| Setting | Kind | Blank or new | What it does |
|---|---|---|---|
| `phone_alerts_service` | ntfy/Pushover | ntfy | Where the alerts go. |
| `ntfy_topic` | text | blank: off | The topic, the same as in the ntfy app. The password: keep it long and random. |
| `ntfy_server` | text | https://ntfy.sh | The server the Pi posts to. Leave it at https://ntfy.sh. |
| `ntfy_token` | text | blank: none | An access token, starting `tk_`, only for a topic reserved with a paid ntfy.sh account. |
| `ntfy_link` | text | blank: none | A web address an alert opens when tapped, such as the brew-day screen: `http://<your Pi>:8000/`. |
| `phone_alerts_timers` | Yes/No | Yes | A brew-day timer that has run out. |
| `phone_alerts_additions` | Yes/No | Yes | A boil addition: the boil step's hop and first-wort reminders. |
| `phone_alerts_questions` | Yes/No | Yes | CraftBeerPi asks something and waits for your answer. |
| `phone_alerts_errors` | Yes/No | Yes | An error. |
| `phone_alerts_warnings` | Yes/No | Yes | A warning. |
| `pushover_user` | text | blank: off | Pushover only: your Pushover user key, 30 letters and digits. |
| `pushover_token` | text | blank: off | Pushover only: the API token of the application you created for CraftBeerPi, 30 letters and digits. |

A change takes effect at once. While the chosen service isn't set up, nothing is sent and nothing is logged as an
error. A setting left blank that has a default counts as that default (`PushOver.blank_defaults()`). An install of
0.0.10.post1 keeps its switches, under their new names, and stays on Pushover if its keys were filled in.

### A test alert

`POST /phone-alerts/test` (or `/pushover/test`, as before) sends a test through the chosen service, whatever the
switches say. `kind`, in the query or as JSON (`{"kind": "timer"}`), says which:

- `alert`, the default: `Test alert from CraftBeerPi through ntfy: phone alerts work.`, sent as an addition's,
  question's or error's alert is: ntfy priority 4, Pushover high priority.
- `timer`: sent as a run-out timer's alert is. On ntfy: priority 5 with 🚨, once. On Pushover: an emergency alert,
  which Pushover repeats once, a minute later, before CraftBeerPi cancels it; acknowledging it on the phone stops it
  sooner.

      curl -X POST http://<your Pi>:8000/phone-alerts/test
      curl -X POST "http://<your Pi>:8000/phone-alerts/test?kind=timer"

The answer is JSON a page can read:

    {"sent": false, "reason": "refused", "field": "topic", "detail": "ntfy answered 403 (40301): forbidden",
     "service": "ntfy", "kind": "alert"}

- `sent`: true or false.
- `reason`: null when sent; otherwise one of the reasons below.
- `field`: the setting at fault, or null when no one setting is: `user` is `pushover_user`; `token` is
  `pushover_token` on Pushover and `ntfy_token` on ntfy; `topic` is `ntfy_topic`.
- `detail`: what happened, in words. When sent, what to expect on the phone.
- `service` and `kind`: what was tested.

| HTTP | `reason` | What it means | `field` |
|---|---|---|---|
| 200 | null | Sent. | null |
| 400 | `bad_request` | `kind` is neither `alert` nor `timer`. | null |
| 409 | `not_set_up` | The chosen service's settings are blank or wrong: a blank or malformed topic, a blank Pushover key, a server that isn't a web address. | `topic`, `user`, `token`, or null for the server |
| 429 | `limit` | The service has had too many alerts lately: ntfy's 42901 (too many at once) or 42908 (the day's 250), Pushover's monthly 10,000. | null |
| 502 | `refused` | The service didn't take it. Pushover's 400s name the key it refused; ntfy answers 401 for an access token it doesn't know and 403 for a topic someone else has reserved. | `user`, `token`, `topic`, or null |
| 502 | `no_devices` | Pushover took it but has no phone to send it to: no phone is signed in to the Pushover app with that account. | `user` |
| 504 | `unreachable` | The service couldn't be reached, didn't answer within 10 seconds, or failed itself (its 5xx). | null |

`GET /phone-alerts/topic` gives a new random topic each time: `cbpi-` and 24 random letters and digits. It isn't kept,
and isn't logged.

### Pushover, the alternative

Pushover needs an account at [pushover.net](https://pushover.net) and its app, free for 30 days from adding the phone,
then a one-time purchase in the app ($4.99). What it adds: a timer's alert repeats on the phone until it's
acknowledged there, every minute for up to 50 minutes (Pushover stops at 50 repeats), and stops once the timer is
answered on a screen. With Critical Alerts on, it sounds with the iPhone on silent. Up to 10,000 alerts a month are
free.

1. Install the Pushover app on the iPhone, and sign up in it, or at pushover.net and then log in in the app. Allow its
   notifications.
2. To have a timer's alert sound with the iPhone on silent, turn on Critical Alerts for emergency priority in the
   app's settings; iOS will ask you to allow them.
3. Your **user key**, 30 letters and digits, is on your dashboard at pushover.net, and in the app's settings.
4. Create an application for CraftBeerPi at [pushover.net/apps/build](https://pushover.net/apps/build), under Your
   Applications on the dashboard. Its page shows its **API token**, 30 letters and digits.
5. In CraftBeerPi's settings: `phone_alerts_service` Pushover, the user key in `pushover_user`, the API token in
   `pushover_token`.
6. Send a test alert, and a test of a timer's alert, as above. Pushover takes an alert even when no phone is signed
   in, and says so; the test reports it as `no_devices`, and an alert sent then is logged as not sent.

### Without the internet

If the Pi can't reach ntfy.sh or Pushover, the alerts are lost. An alert that fails is said once in the log, as a
warning, and isn't tried again. **The screens and the buzzer stay the real alarms.** Sending never holds the brew up:
each alert goes out on its own, and gives up after 10 seconds.

### The topic and the keys

None of them is ever written to the log: the topic, the access token, the Pushover user key and token. CraftBeerPi
writes every setting into the log at debug level; this plugin replaces them there with `[hidden]`.

### For a test bench

In CraftBeerPi's environment, `CBPI_NTFY_SERVER` points it at another ntfy server and `CBPI_PUSHOVER_API` at another
Pushover API, fake ones say: `CBPI_NTFY_SERVER=http://127.0.0.1:8048`, `CBPI_PUSHOVER_API=http://127.0.0.1:8047/1`.

### Changelog:

- 18.11.26: (0.0.10.post2) ntfy, free and with no account, as the default service, Pushover the alternative. A run-out timer on ntfy is sent at priority 5 and again every 2 minutes, 10 more times at most, until answered; the rest at priority 4, each with its emoji. A random topic suggested at /phone-alerts/topic; the test alert at /phone-alerts/test as well, of an alert or a timer's alert, with an answer a page can read. A Pushover emergency alert repeats for up to 50 minutes, Pushover's limit, and one Pushover has no phone for counts as not sent. The switches renamed phone_alerts_*, their values kept.
- 18.11.26: (0.0.10.post1) Phone alerts for what the brew-day screen beeps for: run-out timers as emergency alerts, cancelled when answered on any screen; additions, questions, errors and warnings as high-priority alerts. A switch for each kind, a test alert, sent with a 10 second timeout without holding CraftBeerPi up, the keys kept out of the log.
- 10.10.25: (0.0.10) Error handling during message sending via request
- 17.01.25: (0.0.9) Allow HTML in message
- 10.06.23: (0.0.8) bump version to release
- 02.06.23: (0.0.8.rc1) added cbpi4 requirement
- 05.04.23: (0.0.8.a2) test for plugin settings selection test branch
- 10.05.22: (0.0.7) Updated README (removed cbpi add)
- 10.05.22: (0.0.4) Removed cbpi dependency
- 02.04.21:  Minor changes
- 15.03.21: Initial Release.
