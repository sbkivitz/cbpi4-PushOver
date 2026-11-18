# Craftbeerpi4 PushOver Notifications Plugin

## Phone alerts, through the Pushover service, for everything the brew-day screen beeps for

CraftBeerPi sends the alerts itself, from the Raspberry Pi, so they reach your phone with no browser open.

### Software installation:

Please have a look at the [Craftbeerpi4 Documentation](https://openbrewing.gitbook.io/craftbeerpi4_support/readme/plugin-installation)

- Package name: cbpi4-PushOver
- Package link: https://github.com/PiBrewing/cbpi4-PushOver/archive/main.zip

This version needs a CraftBeerPi with the brew-day timers and the brewing clock (`cbpi.api.clock`).

### What is sent

One push for each of these, as it happens:

| The brew-day screen | What CraftBeerPi raises | The push | Pushover priority |
|---|---|---|---|
| A timer that has run out: its alarm sounds again and again until it's answered | The timer's prompt, titled with its name: "The 15:00 timer has run out." | `Timer HOP STAND: time's up` | Emergency (2): repeated every 60 s for up to 1 hour, until you acknowledge it on the phone |
| An addition: two beeps | The boil step's Hop Alert (Hop_1 to Hop_6) and its first-wort hops, and the first-wort hops asked for as the runoff starts | `Boil: add 1.50 oz Citra now` | High (1), one alert |
| A question: two beeps | Any other notification with a button to answer it | `CraftBeerPi asks: Resume brew? ...` | High (1), one alert |
| An error: two beeps | An error with no buttons: a step that failed, an element on by hand past its target, ... | `Error: Brew Steps: Step 'Mash' failed and the profile has stopped. ...` | High (1), one alert |
| A warning: two beeps | A warning with no buttons | `Warning: ...` | High (1), one alert |

Nothing else is sent: the screen makes no sound for information and success messages.

The title of each push is your brewery's name (the BREWERY_NAME setting), so the rig's alerts can't be taken for a
simulator's. An addition starts with the name of the step running, `Boil` above.

A timer's emergency alert stops on the phone as soon as the timer is answered on any screen - OK, +5 MIN, a new time,
or removed: CraftBeerPi cancels it through Pushover's receipts API. Acknowledging it on the phone stops it on the
phone only. The screens keep ringing until the timer is answered there.

Each alert is sent once:

- Reloading a page, or a page reconnecting, sends nothing: the page is shown what still waits, with no new alert.
- After a restart, a timer brought back run out isn't sent again. Its alert from before still repeats on the phone,
  and still stops when the timer is answered. Which timers' alerts still repeat is kept in `pushover.json` in the
  config folder for that.
- The same error or warning again within 10 minutes isn't sent again, as the screen shows the same words once.
- A question CraftBeerPi asks again after a restart (Resume brew?, add the malt, ...) is a new question, and a new
  alert.

### Turning it on

Nothing is sent until both keys are filled in.

1. Make an account at [pushover.net](https://pushover.net), and install the Pushover app on your iPhone. Log in to the
   app with the same account and allow its notifications. (Pushover is free for 30 days, then a one-time purchase in
   the app.) To have a run-out timer's alarm sound with the iPhone on silent, turn on Critical Alerts for emergency
   priority in the app's settings.
2. Your **user key** is at the top of your dashboard on pushover.net, once you've logged in.
3. Still on pushover.net, under Your Applications, choose **Create an Application/API Token**. Call it CraftBeerPi,
   tick the terms, and create it. Its page shows its **API token**.
4. In CraftBeerPi, on the Settings page, among the plugin settings for cbpi4-PushOver: paste the user key into
   `pushover_user` and the API token into `pushover_token`, and save. No restart is needed.
5. Send a test alert (below).

### The settings

| Setting | Kind | Blank or new | What it does |
|---|---|---|---|
| `pushover_user` | text | blank: off | Your Pushover user key. |
| `pushover_token` | text | blank: off | The API token of the application you created for CraftBeerPi. |
| `pushover_timers` | Yes/No | Yes | A brew-day timer that has run out. |
| `pushover_additions` | Yes/No | Yes | A boil addition: the boil step's hop and first-wort reminders. |
| `pushover_questions` | Yes/No | Yes | CraftBeerPi asks something and waits for your answer. |
| `pushover_errors` | Yes/No | Yes | An error. |
| `pushover_warnings` | Yes/No | Yes | A warning. |

A change takes effect at once. With both keys blank, or one of them, nothing is sent and nothing is logged as an
error. A switch left blank counts as Yes (`PushOver.blank_defaults()`).

### A test alert

`POST /pushover/test` sends `Test alert from CraftBeerPi: phone alerts work.`, whatever the switches say. From a
computer on the same network:

    curl -X POST http://<your Pi>:8000/pushover/test

It answers `{"sent": true}`; 409 with the keys blank; 502 if Pushover didn't take it, with why in `error` - a
mistyped key, say.

### Without the internet

If the Pi can't reach Pushover, the alerts are lost. A push that fails is said once in the log, as a warning, and
isn't tried again. **The screens and the buzzer stay the real alarms.** Sending never holds the brew up: each push
goes out on its own, and gives up after 10 seconds.

### The key and the token

Neither is ever written to the log. CraftBeerPi writes every setting into the log at debug level; this plugin
replaces the two keys there with `[hidden]`.

### For a test bench

`CBPI_PUSHOVER_API` in CraftBeerPi's environment points it at another Pushover API, a fake one say:
`CBPI_PUSHOVER_API=http://127.0.0.1:8047/1`. Without it, alerts go to `https://api.pushover.net/1`.

### Changelog:

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
