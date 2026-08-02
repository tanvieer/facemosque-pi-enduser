# Alexa skill — ask the mosque about prayer times

> "Alexa, ask my mosque when the next prayer is."
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
Alexa, ask my mosque when the next prayer is
Alexa, ask my mosque when Fajr is
Alexa, ask my mosque what today's prayer times are
Alexa, ask my mosque when jummah is
Alexa, ask my mosque when iqamah is for Maghrib
Alexa, open my mosque                       ← then just ask
```

## Install

### 1. Create the skill

At [developer.amazon.com/alexa](https://developer.amazon.com/alexa) →
*Alexa Skills Kit* → **Create Skill**:

- **Name:** `My Mosque`
- **Model:** Custom
- **Hosting:** Alexa-hosted (Python)

### 2. Interaction model

*Build* tab → *Interaction Model* → **JSON Editor**. Replace everything with
[`interaction-model.json`](interaction-model.json), then **Save**, then
**Build model**.

### 3. Code

*Code* tab:

- Replace `lambda_function.py` with [`lambda_function.py`](lambda_function.py)
- Add a new file `prayer-times.json` — paste in the contents of
  [`../data/mosque-4.json`](../data/mosque-4.json)
- **Deploy**

Leave `requirements.txt` alone. The skill is standard library only, so nothing
in it is used.

### 4. Try it

*Test* tab → switch from *Off* to **Development**, then type
`ask my mosque when the next prayer is`.

It is already on your Echo devices at this point, as long as they are on the
same Amazon account as the developer account.

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
