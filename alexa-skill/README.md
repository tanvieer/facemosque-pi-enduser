# Alexa skill — ask the mosque about prayer times

> "Alexa, ask mosque when the next prayer is."
> *"The next prayer is Maghrib at 9:01 PM, in 3 hours and 1 minute."*

A voice front end for the same prayer times the Pi plays. It runs entirely in
Amazon's cloud and answers from a copy of `data/mosque-4.json`, so it needs no
Raspberry Pi, no API key in the cloud, and no network call at answer time —
which keeps every reply inside Alexa's 8 second budget.

Free: [Alexa-hosted skills](https://developer.amazon.com/en-US/docs/alexa/hosted-skills/usage-limits.html)
give unlimited Lambda requests, 25 GB S3 and 25 GB DynamoDB per developer
account. It never needs publishing or certifying — a skill in development
works on your own Echo devices indefinitely.

## What you can ask

```
Alexa, ask mosque when the next prayer is
Alexa, ask mosque when Fajr is
Alexa, ask mosque what today's prayer times are
Alexa, ask mosque when jummah is
Alexa, ask mosque when iqamah is for Maghrib
```

The short forms work too, which is what you end up using:

```
Alexa, ask mosque next
Alexa, ask mosque isha
Alexa, ask mosque maghrib iqamah
Alexa, ask mosque times
Alexa, ask mosque jummah
Alexa, open mosque              ← answers, then leaves the mic open
```

Amazon requires the invocation name every time a skill is addressed cold, so
`Alexa, next prayer` cannot work on its own. An
[Alexa Routine](https://www.amazon.com/alexa-routines) closes that gap: trigger
it on the phrase you want and give it the custom action
`ask mosque when the next prayer is`.

## Tutorial: put this on your own Alexa

About fifteen minutes, and it costs nothing. You never publish the skill and it
never goes through certification — a skill left in development works on your own
Echo devices for as long as you keep it.

### Before you start

**Use the Amazon account your Echo is signed in to.** This is the one step that
is painful to undo. A development-stage skill appears only on the developer
account's own devices, so a skill built under a different address will work
perfectly in the browser simulator and be invisible to the Echo in your kitchen.

Check which account that is in the Alexa app → *More* → *Settings* → *Your
Account*, and sign in to the developer console with the same one.

While you are in the app, note your Echo's language: *Devices* → your Echo →
*Language*. You need it in the next step.

### 1. Create the skill

[developer.amazon.com/alexa](https://developer.amazon.com/alexa) → *Alexa Skills
Kit* → **Create Skill**.

| Field | Value |
| --- | --- |
| Skill name | `Facemosque` — only a label in the console, say anything you like |
| Primary locale | **whatever your Echo's language is** |
| Type of experience | Other |
| Model | **Custom** |
| Hosting service | Alexa-hosted (Python) |
| Hosting region | the one nearest you — EU (Ireland) from Europe |
| Template | Start from Scratch |

Model and hosting region cannot be changed afterwards; everything else can. Get
the locale wrong and the skill simply will not answer, though you can add a
second locale later rather than starting over.

**Create Skill**, then wait a minute or two while Amazon builds the backend.

### 2. Interaction model

This is the half that decides what Alexa listens for.

*Build* tab → **Interaction Model** in the left sidebar → **JSON Editor**.
Click in the editor, select all, and paste
[`interaction-model.json`](interaction-model.json) over it.

**Save**, then **Build skill** — saving alone changes nothing. Wait for *Build
successful*; a minute is normal.

*Slot Types* in the sidebar should now read **(1)** and list `PRAYER`. If it
still says (0) the paste did not take.

### 3. Code

This is the half that decides what Alexa answers.

*Code* tab. The file tree on the left shows a `lambda/` folder.

1. Open `lambda_function.py`, select all, paste
   [`lambda_function.py`](lambda_function.py) over it.
2. **New File** → name it exactly `prayer-times.json`, **inside `lambda/`**,
   next to `lambda_function.py`. The code looks for it beside itself, so a file
   one level up will not be found.
3. Paste your mosque's timetable into it — see *Use your own mosque* below.
   It is about 120 KB, so the browser may freeze for a few seconds mid-paste.
4. Leave `requirements.txt` and `utils.py` alone. The skill is standard library
   only, so nothing gets installed.

**Save**, then **Deploy**. Another minute or two.

### 4. Test in the browser

*Test* tab → the dropdown at the top left, **Off → Development**. Nothing works
until you do this.

Type `ask mosque when the next prayer is` and you should get a spoken answer
plus the raw JSON on the right.

Typing skips speech recognition entirely, so a green result here proves the code
works — not that Alexa can hear you.

### 5. Test on the Echo

There is nothing to enable or install. The skill is already on every Echo signed
in to that Amazon account.

> "Alexa, ask mosque when the next prayer is"

If she takes the question but not the name, see *Troubleshooting*.

### 6. Shorter phrases, with a Routine

Amazon requires the invocation name whenever a skill is addressed cold, so
`Alexa, next prayer` cannot work by itself. A Routine gets you there — it says
the long sentence on your behalf.

Alexa app → *More* → **Routines** → **+**

- *When this happens* → **Voice** → `prayer time`
- *Add action* → **Custom** → `ask mosque when the next prayer is`

Now **"Alexa, prayer time"** is enough. Make one per question — `jummah`,
`today's prayers`, whatever you actually say out loud.

### Use your own mosque

The repo ships mosque 4 (Takaful, Chemnitz) in
[`../data/mosque-4.json`](../data/mosque-4.json). For any other mosque, on the
Pi or anywhere the CLI runs:

```sh
adhanctl set mosque <id>
adhanctl bundle          # writes data/mosque-<id>.json
```

Paste that file into `prayer-times.json` and **Deploy**.

One caveat: `berlin_now()` implements the EU daylight-saving rule. A mosque
outside the EU needs the change described under *Notes*.

### Changing the invocation name

`"invocationName"` at the top of the interaction model, or *Build* → *Invocations*
→ *Skill Invocation Name*. **Save** and **Build skill** afterwards.

Amazon wants two or more words unless the name is distinctive enough to stand
alone, and Alexa mishears short generic words more often than you would expect.
If `mosque` gives you trouble, `my mosque` and `the mosque` are barely longer.

### Troubleshooting

| What you see | What it is |
| --- | --- |
| Echo: *"I don't know that one"* | The skill is on a different Amazon account, or testing is still set to *Off* |
| Simulator answers, Echo does not | Locale mismatch — add your Echo's language under *Build* → *Languages* |
| Alexa mishears the invocation name | Rename it to two words |
| *"There was a problem with the requested skill's response"* | The code raised. *Code* tab → **CloudWatch Logs** has the traceback |
| *"I do not have any prayer times loaded"* | `prayer-times.json` is missing, empty, or outside `lambda/` |
| Every time is an hour out | Daylight saving — `berlin_now()` is EU-only |
| *"These are last year's times"* | Working as intended; refresh the bundle, below |

## Keeping it current

The times run to the end of the bundled year. **After that the skill keeps
working** — it falls back to the same calendar date in the year it has, and
says so:

> "The next prayer is Maghrib at 9:01 PM. These are last year's times, so they
> may be a minute or two out."

Prayer times shift by a minute or two year to year, so that is a much better
answer than silence. To refresh, once the mosque publishes the new year:

```sh
adhanctl bundle          # rewrites data/mosque-4.json
```

then paste the new contents into `prayer-times.json` and **Deploy**. Once a
year.

## Notes

- **Friday**: the jummah adhan replaces Dhuhr, exactly as the Pi plays it. The
  two have to agree — being told one time and hearing the adhan at another
  would be worse than not asking.
- **Friday is taken from the real date**, never from the year the times were
  borrowed from: 15 March is a Sunday one year and a Tuesday the next.
- **Timezone** is worked out in `berlin_now()` rather than with `zoneinfo`,
  because AWS Lambda images ship without the tz database and
  `ZoneInfo("Europe/Berlin")` raises unless you add `tzdata`. The EU rule is
  ten lines and this keeps the skill dependency-free. It was checked against
  `zoneinfo` for all 26,304 hours of 2026–2028 with no mismatch. A mosque
  outside the EU should swap it for `zoneinfo` and add `tzdata` to
  `requirements.txt`.
- **`prayer-times.json` is not committed here** — it is a copy of
  `data/mosque-4.json`, and one canonical copy is better than two that drift.
