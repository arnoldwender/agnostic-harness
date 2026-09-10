# The Maxims

> The first utterance of the Agnostic Harness. Before any agent acts, the harness opens
> with a fixed maxim and then a rotating *maxim of the day* — public-domain wisdom on craft,
> judgment, honesty, and perseverance. It is the secular twin of a blessing: an opening word
> to steady the work.

## The opening maxim (fixed)

```text
  "No longer talk at all about the kind of man that a good man ought
   to be, but be such."
                          — Marcus Aurelius, Meditations 10.16 (tr. George Long)
```

Chosen because it *is* the Navigator's fourth rule in one line: done is what you do, not what
you declare.

Recorded here as an attributed quotation, and not only inside the banner above, so that
[`gate/citations.py`](gate/citations.py) actually reads it. A line that lives only in a fenced
code block is invisible to the gate, and this is the one line in the repo that has already been
wrong once — the banner used to carry a condensation nobody published, under George Long's name:

> *No longer talk at all about the kind of man that a good man ought to be, but be such.* — Marcus Aurelius, Meditations X.16 (tr. George Long, 1862)

## The rotating maxim

Beneath the fixed opening, the harness prints one rotating line from
[`maxims.txt`](maxims.txt) — a maxim of the day, changing daily. The opening never changes;
the maxim rotates. Edit [`maxims.txt`](maxims.txt) (one `Maxim — Author` per line) to curate
or extend the pool.

Every line has a provenance file in [`sources/`](sources/) carrying the work, the author's
dates, the translator, and the public-domain status **per jurisdiction** — and the honest
answer is not a uniform yes. An earlier version of this page claimed the whole pool was public
domain. Measured 2026-09-10, that was wrong in two ways, both recorded rather than hidden:

- **Two lines rest on translations still under EU copyright.** The Sun Tzu is Lionel Giles'
  1910 English (Giles died 1958 → free in the EU in 2029) and the Seneca 76 is Gummere's Loeb
  (Gummere died 1969 → 2040). Both are public domain in the US, which is what the sites
  hosting them are going by. Arnold publishes from Germany.
- **Seven lines are condensations, not quotations from an edition.** They render the original
  faithfully but match no published translation located, so they are marked
  `provenance: unverified` with a note naming what the real translators actually wrote.

The current pool:

> - *It is not because things are difficult that we do not dare; it is because we do not dare that things are difficult.* — Seneca
> - *As long as you live, keep learning how to live.* — Seneca
> - *It is impossible for a man to learn what he thinks he already knows.* — Epictetus
> - *No great thing is created suddenly.* — Epictetus
> - *When you know a thing, to hold that you know it; and when you do not know a thing, to allow that you do not know it — this is knowledge.* — Confucius
> - *The superior man is modest in his speech, but exceeds in his actions.* — Confucius
> - *If you know the enemy and know yourself, you need not fear the result of a hundred battles.* — Sun Tzu
> - *Any man can make a mistake, but only a fool persists in his error.* — Cicero
> - *Read not to contradict and confute; nor to believe and take for granted; nor to find talk and discourse; but to weigh and consider.* — Francis Bacon
> - *Truth is the daughter of time, not of authority.* — Francis Bacon
> - *A man's character is his fate.* — Heraclitus
> - *Much learning does not teach understanding.* — Heraclitus

*The harness emits this first, on startup — [`bin/maxim`](bin/maxim).*
