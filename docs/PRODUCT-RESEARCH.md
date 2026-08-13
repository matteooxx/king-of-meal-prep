# Product And Satisfaction Research

Research sampled on 2026-07-18 for the plan, cook, shop, pantry, and recipe
workflows in King of Meal Prep.

## Method

The audit combined:

- a complete walkthrough of King's current routes, state transitions, planner
  scoring, pantry and prepared ledgers, capture flows, and offline behavior;
- US App Store metadata and recent-review feeds for fifteen meal, recipe,
  grocery, and nutrition products;
- public GitHub adoption signals for three established self-hosted products;
- recurring positive and negative themes, not isolated feature requests.

App Store ratings are directional satisfaction signals, not controlled
experiments. Review feeds overrepresent users motivated to write, ratings move
over time, and GitHub stars measure developer interest rather than retention.
No competitor branding, assets, source code, or proprietary visual treatment
was copied. King adopts general interaction patterns and implements them
against its own data model.

## Satisfaction Snapshot

| Product | US iOS rating | Ratings |
| --- | ---: | ---: |
| Paprika | 4.899 | 53k |
| AnyList | 4.869 | 79k |
| Plan to Eat | 4.842 | 6k |
| Recipe Keeper | 4.840 | 29k |
| MacroFactor | 4.835 | 19k |
| Crouton | 4.819 | 2.7k |
| Mealime | 4.812 | 53k |
| ReciMe | 4.796 | 260k |
| Samsung Food | 4.781 | 6.3k |
| Pestle | 4.735 | 1.7k |
| Eat This Much | 4.716 | 22k |

Self-hosted adoption at the same sample date: Mealie 12.7k GitHub stars, Grocy
9.2k, and Tandoor Recipes 8.5k.

## What Users Repeatedly Value

1. Fast capture. URL, share-sheet, text, photo, and receipt paths are valued
   when the result is reviewable instead of silently wrong.
2. Reliable shopping. Users trust automatic lists when quantities and
   categories are predictable, checks survive poor connectivity, and household
   members can act quickly.
3. Less deciding. A weekly plan is satisfying when it reduces choice without
   becoming repetitive or ignoring pantry, budget, schedule, and preferences.
4. Help during cooking. The strongest cooking surfaces show one clear step,
   quantities needed now, large navigation, timers, and a screen that stays on.
5. Ownership. Offline access, export, self-hosting, no ads, and stable pricing
   are recurring reasons for loyalty.
6. Neutral adaptation. Users respond better when actual behavior improves the
   next recommendation without streak loss, scolding, or opaque corrections.

## Recurring Sources Of Frustration

- auto-plans that are repetitive, implausible, or impossible to explain;
- plans that ignore pantry stock, disliked meals, budget, or available time;
- pantry systems whose manual upkeep costs more time than they save;
- broken recipe imports, especially photo/PDF and heavily scripted sites;
- grocery quantities or categories that require a second manual list;
- recipe collectors that look polished but provide little help at the stove;
- no rating or rejection signal, so disliked recipes keep returning;
- subscription or paywall regressions and weak data portability.

## Nutrition Logging Follow-Up

A second pass focused on nutrient capture and daily logging after live data
showed imported recipes and scanned products with zero totals.

| Product | US iOS rating | Ratings |
| --- | ---: | ---: |
| MacroFactor | 4.835 | 19k |
| Mealime | 4.812 | 53k |
| Samsung Food | 4.781 | 6.3k |
| Cronometer | 4.773 | 95k |
| Lose It! | 4.766 | 766k |
| FoodNoms | 4.739 | 7.4k |
| MyFitnessPal | 4.713 | 2.3m |

Recent-review themes were consistent across these products:

1. Direct grams beat serving-fraction arithmetic. Users want to weigh an
   amount and see the result, not reverse-engineer "0.73 servings."
2. Barcode capture must remain reviewable. Speed creates satisfaction only
   when the label values, source, and selected product stay visible.
3. Custom and regional foods must persist. Losing a corrected barcode or
   replacing it with a generic database match breaks trust.
4. Repeated logging should take very few taps. Recent foods, saved foods, and
   pantry products are stronger defaults than an empty free-text form.
5. The full day should remain visible while logging. Users dislike navigation
   churn and workflows that hide totals after every entry.
6. Missing data must look missing. Silent zeroes and confident but incorrect
   matches are more damaging than an explicit request for an amount or match.

King therefore keeps multiline recipe capture, adds a structured nutrition
review before save, calculates selected food profiles from the entered amount,
persists barcode label nutrition on pantry items, and offers pantry-first daily
logging without silently changing stock.

## Pattern Selection

| Pattern | Strong examples | Decision for King |
| --- | --- | --- |
| Reviewable multi-path capture | ReciMe, Samsung Food, Recipe Keeper | Already covered by the candidate recognition inbox, receipt review, barcode fallback, and provenance work. |
| Deterministic grocery list | AnyList, Paprika, Mealime | Keep King's yield-aware subtraction and offline checks. Manual shared extras remain a later, separate scope. |
| Low-decision weekly plan | Mealime, Plan to Eat, Eat This Much | Retain deterministic constraints; add visible pick reasons rather than a black-box score. |
| One-step cooking | Crouton, Pestle, Paprika, Recipe Keeper | Add a dedicated full-screen route with scaled ingredients, inline reminders, timers, Wake Lock, and resume. |
| Meal preference memory | Paprika, Recipe Keeper | Add a 1-5 rating plus explicit make-again and avoid intent. |
| Adherence-neutral adaptation | MacroFactor | Feed actual preference back into ranking, never shame an omitted rating, and never bypass safety constraints. |
| Local ownership | Mealie, Tandoor, Grocy | Preserve self-hosting and include feedback in portable exports and encrypted backups. |

## Implemented Outcome

The chosen release closes one coherent loop:

1. The planner proposes meals and explains each generated choice.
2. Fresh meals open guided cooking; prepared portions retain direct logging.
3. Guided progress, ingredient checks, batch scaling, and timers survive reload.
4. Completion still executes King's existing atomic pantry/prepared transaction.
5. An optional post-meal prompt records rating and make-again/avoid intent.
6. The next proposal excludes explicit avoids and modestly favors proven meals.

This scope was selected over adding more capture paths because the candidate
release already addresses King's largest capture, trust, and ownership gaps.
It was selected over manual shopping extras because cooking guidance and a
closed feedback loop address a more consequential break in the daily workflow.

## Sources

App Store metadata:

- `https://itunes.apple.com/lookup?id=1303222868&country=us` - Paprika
- `https://itunes.apple.com/lookup?id=522167641&country=us` - AnyList
- `https://itunes.apple.com/lookup?id=1215348056&country=us` - Plan to Eat
- `https://itunes.apple.com/lookup?id=974683711&country=us` - Recipe Keeper
- `https://itunes.apple.com/lookup?id=1553503471&country=us` - MacroFactor
- `https://itunes.apple.com/lookup?id=1461650987&country=us` - Crouton
- `https://itunes.apple.com/lookup?id=1079999103&country=us` - Mealime
- `https://itunes.apple.com/lookup?id=1593779280&country=us` - ReciMe
- `https://itunes.apple.com/lookup?id=1133637674&country=us` - Samsung Food
- `https://itunes.apple.com/lookup?id=1574776971&country=us` - Pestle
- `https://itunes.apple.com/lookup?id=981637806&country=us` - Eat This Much
- `https://itunes.apple.com/lookup?id=1145935738&country=us` - Cronometer
- `https://itunes.apple.com/lookup?id=1479461686&country=us` - FoodNoms
- `https://itunes.apple.com/lookup?id=341232718&country=us` - MyFitnessPal
- `https://itunes.apple.com/lookup?id=297368629&country=us` - Lose It!

Recent-review feeds used the public Apple endpoint:

`https://itunes.apple.com/us/rss/customerreviews/page=1/id=<APP_ID>/sortby=mostrecent/json`

Self-hosted projects:

- `https://github.com/mealie-recipes/mealie`
- `https://github.com/grocy/grocy`
- `https://github.com/TandoorRecipes/recipes`
