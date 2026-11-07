# Plant Genomics Funding Monitor: Project Plan

Exported 7 Nov 2026 from the plan doc, which remains the living version. The section "Prototype decisions" at the end records choices made while setting up the prototype.

## Summary

We build a small, focused version of [ELFI](https://www.elfi.info/ueber_elfi.php) that tracks funding opportunities for plant genomics research, for researchers at IPK Gatersleben.

A daily job checks a curated list of funders and programmes. When a page changes, a language model extracts any new call into a structured results table. Researchers search and filter that table, and every entry links back to the original call.

The plan has three stages. Version 1 delivers the source list, the daily pipeline and a searchable table. Version 2 adds email alerts, saved research profiles and a review workflow. Later versions add deeper relevance matching and integrations. Running costs stay small at every stage; the main cost is staff time to curate sources and check entries.

## Context and users

The tool serves [IPK Gatersleben](https://www.ipk-gatersleben.de/en/), a Leibniz institute for plant genetics and crop plant research, so its scope can follow IPK's research profile rather than all of science.

IPK's research is organised into four departments (Genebank, Breeding Research, Molecular Genetics, Physiology and Cell Biology) plus independent groups, including Biotechnology and Genome Editing. Its infrastructure covers the federal ex situ genebank, plant phenotyping, sequencing and bioinformatics. These areas define both the source list and the tag vocabulary.

There are three user groups:

- **Researchers** (group leaders, postdocs, PhD students) who search for calls that fit their work and career stage.
- **The third-party funding office.** IPK's [Finance, Knowledge Transfer and Third Party Funding](https://www.ipk-gatersleben.de/en/institute/administration/finance-knowledge-transfer-and-third-party-funding) group already supports applications. It is the natural owner of the tool and the reviewer of new entries.
- **Institute management,** who want an overview of upcoming opportunities across departments.

ELFI's breadth is not needed here. It covers around 14,000 programmes across 260 research fields. IPK needs depth in one field: catching calls relevant to plant genomics, including topics hidden inside broad programmes.

## How it works

A scheduled job runs once a day, and the language model is called only for pages that changed. That keeps costs to a few euros a month.

The daily pipeline:

1. **Sources table:** 30 to 50 funder pages, kept by people.
2. **Daily fetch:** each page, feed or API, once a day.
3. **Page changed?** If not, skip until tomorrow (no model call, no cost).
4. **Screen with Claude Haiku 5.5:** is this a new call, and is it relevant to plant genomics? If not, drop it and log it (kept for checking misses).
5. **Extract with Claude Sonnet 5.5:** dates, eligibility, summary, tags.
6. **Results table:** de-duplicated, marked as new. The funding office checks new entries in a weekly review (approve, edit, reject).
7. **Search page:** Datasette (search, filter, export).

Most pages are unchanged on a given day and cost nothing; the two model steps handle the rest, and nothing counts as checked until a person has reviewed it.

### Where the language model fits

The model does two jobs, and a normal program does everything else (fetching, change detection, saving, searching).

1. **Screening, with Claude Haiku 5.5.** This is a small, fast and very cheap model. It reads the changed part of a page and answers two questions: is this a new or updated funding call, or just news? Is it plausibly relevant to plant genomics? Most changes stop here.
2. **Extraction, with Claude Sonnet 5.5.** This is a more careful model. It reads the full call text, including linked PDFs, and fills in the fields of the results table: title, dates, eligibility, amount, a summary in English and German, tags from the fixed list and a relevance score. It also returns the exact sentences each date came from, so the reviewer can check them in seconds.

### How the script calls the model

- The script sends each page to the Claude API with fixed instructions and asks for the answer in a fixed format (JSON) that matches the results table.
- The script then checks the answer before saving: dates must be valid, tags must come from the vocabulary, and the link must point to the source.
- It compares new entries with existing ones by link and title. If a call already exists, it updates the status (for example, deadline extended) instead of adding a duplicate.
- Extraction can use the Batch API at half price, because results are needed within a day, not within seconds.
- The model never browses the web freely. It only sees pages from the sources table, which keeps it predictable and cheap.

Corrections from the weekly review are added to the instructions as examples, so the same mistakes become rarer over time.

## Data model

Two core tables carry V1: a sources table that the team curates, and a results table that the pipeline fills. A small tag vocabulary keeps the results searchable.

### Sources table (curated by people)

| Field | Example | Purpose |
| --- | --- | --- |
| Funder | Deutsche Forschungsgemeinschaft (DFG) | Who funds |
| Programme or page | DFG calls for proposals | What is watched |
| URL | Link to the page, feed or API | Where the job looks |
| Access method | RSS, API, HTML page, PDF | How the job reads it |
| Scope | National, EU, international | For filtering and reporting |
| Check frequency | Daily, weekly | Slow pages need not be checked daily |
| Last content hash | Fingerprint of the last version | Detects changes cheaply |
| Last checked / last changed | Dates | Shows stale or broken sources |
| Status | Active, broken, paused | Flags sources that need fixing |
| Notes | "Calls listed in a table at the bottom" | Tips for whoever maintains it |

### Results table (filled by the pipeline)

| Field | Example | Notes |
| --- | --- | --- |
| Title | Name of the call | As the funder writes it |
| Funder and programme | Link to the sources table | |
| Original link | URL of the call text | Always shown, so users can verify |
| Call type | Deadline-bound or always open | Always-open programmes, such as DFG research grants, have no deadline |
| Opening date / deadline | Dates | Deadline may have several stages (pre-proposal, full proposal) |
| Summary | 2 to 4 sentences, in English and German | Written by the model, in its own words |
| Eligibility | Country, career stage, institution type | Structured fields, so they can be filtered |
| Funding amount and duration | Up to EUR 1.5 million, 5 years | Free text where the call is vague |
| Tags | From the fixed vocabulary below | |
| Relevance score | 1 to 5 | The model's judgement of fit to plant genomics |
| Review status | New, checked, rejected | Set by the human reviewer |
| Status | Open, closed, extended, withdrawn | Updated when the source changes |
| First seen / last verified | Dates | Shows how fresh an entry is |
| Evidence | The source text the fields came from | Lets the reviewer check facts quickly |

### Tag vocabulary

**Current list:** `tags.csv` in this repository is now the current tag vocabulary. Where it differs from the list below, `tags.csv` applies. It also gives each tag an ID and a description, and sets how many tags from each group a call can have. Scope is deliberately not a tag. Each call takes its scope (national, EU, international) from its source in `sources.csv`, and who may apply (country, institution type) goes in the eligibility fields of the results table.

Tags come from a fixed list, so the same thing is never tagged three different ways. The model may only pick from it; new tags are added by the team.

- **Research area:** genetic resources and genebanks, domestication, breeding and quantitative genetics, genome editing and biotechnology, chromosome and cell biology, plant immunity, phenotyping, seeds and reproduction, nutrition and roots, bioinformatics and data.
- **Organism:** barley, wheat, rye, other cereals, legumes, model plants, other crops.
- **Funding type:** project grant, collaborative project, fellowship, early-career group, infrastructure, travel or exchange, prize, conference.
- **Career stage:** PhD, postdoc, group leader, senior researcher.
- **Scope:** national, EU, international.

## Version 1: searchable results table

V1 proves that a daily pipeline can find relevant calls reliably, using 30 to 50 hand-picked sources and a simple search page, in about 4 to 6 weeks.

**In scope**

- A curated sources table with 30 to 50 high-value funders and programmes.
- The daily pipeline: change detection, model extraction, de-duplication, results table.
- A search page with full-text search and filters by tag, deadline, funder and call type.
- Summaries in English and German.
- A weekly review by the funding office: 20 to 30 minutes to check new entries.
- A simple health report listing sources that failed or haven't changed in a long time.

**Out of scope:** user accounts, saved searches and email alerts (all V2).

**Starter sources** (to be confirmed with IPK researchers and the funding office):

- **National:** DFG; BMFTR (formerly BMBF) and its project agencies; the federal agriculture agency BLE; FNR; the Leibniz Association's own funding lines; the Saxony-Anhalt science ministry.
- **EU:** the Funding & Tenders portal (Horizon Europe Cluster 6, ERC, MSCA) and European partnerships relevant to plants and agriculture.
- **Foundations and exchange:** VolkswagenStiftung, Humboldt Foundation, DAAD, EMBO.
- **International:** UKRI/BBSRC and NSF joint calls open to German partners, and other bilateral programmes.

**Technical stack**

- A Python script runs once a day, on an IPK server or as a scheduled GitHub Actions job.
- An SQLite database holds both tables.
- [Datasette](https://docs.datasette.io/en/stable/_sources/publish.rst.txt), an open-source tool, turns the database into a searchable website with filters and an API. It runs on an IPK server as a Docker container, or on a small cloud host.
- The Claude API does the extraction (see How it works).

**Suggested success criteria after two months of use**

- The tool catches at least 90% of the relevant calls the funding office found by other means in the same period.
- No wrong deadline survives the weekly review.
- At least 10 researchers use the search page in a typical month.

**Effort:** about 15 to 25 developer days to build, plus 5 to 10 days from a domain expert to build the source list and tag vocabulary.

## Version 2: alerts and profiles

V2 brings the calls to researchers instead of waiting for them to search, through saved profiles and email alerts. It starts once V1 meets its success criteria.

**New features**

- **Saved profiles.** Each researcher picks tags, career stage, funding types and free-text keywords.
- **Email alerts.** A weekly digest of new matching calls, plus an optional immediate alert for calls the model rates highly relevant.
- **Deadline reminders.** Researchers can bookmark a call and get reminders, for example 4 weeks and 1 week before the deadline.
- **Review queue.** A simple screen where the funding office approves, edits or rejects new entries. Their corrections are fed back into the model's instructions as examples.
- **More sources.** Grow from 30 to 50 sources to 100 to 200, guided by what V1 missed.
- **Always-open programmes page.** A separate view of permanent programmes, so they don't clutter the list of new calls.

**Technical changes**

- Login with IPK accounts (single sign-on), so profiles belong to staff and no new passwords are needed.
- Datasette stays for searching. A small web app (for example Django or FastAPI) handles profiles, bookmarks and the review queue, because these need users to save data.
- Emails go out through IPK's own mail server or a transactional email service.

**Data protection:** profiles and email addresses are personal data. Hosting at IPK or with an EU provider, opt-in alerts and a short privacy notice cover this; IPK's data protection officer should confirm.

**Effort:** about 15 to 25 further developer days.

## Later iterations (V3 and beyond)

Later versions are options, not commitments; which ones to build depends on how V2 is used. Each could be built on its own in about 10 to 30 developer days.

- **Matching calls to research groups.** Build a profile for each group from its publications (for example via ORCID or IPK's publication list) and suggest calls to the groups they fit best, without anyone setting up a profile by hand.
- **Calendar and intranet integration.** Export deadlines as a calendar feed and show upcoming calls on the IPK intranet.
- **Application tracking.** Record which calls IPK applied to and with what result, so management can see where applications go and which programmes succeed.
- **Partner search for consortium calls.** For collaborative calls, suggest past co-authors or partner institutes who fit the topic.
- **Sharing with other institutes.** Offer the tool to other plant science institutes, for example within the Leibniz Association, and share the cost of curating sources.

## Estimated costs

Running costs stay below about €850 a year even in later versions; staff time to build and curate is by far the largest cost. Datasette itself is free, open-source software, so there is no licence fee.

| Cost item | V1 | V2 | V3+ |
| --- | --- | --- | --- |
| Software licences | €0 (all open source) | €0 | €0 |
| Hosting | €0 on an IPK server, or about €5 to €10 a month on a small EU cloud server | Same as V1 | Up to about €20 a month if a larger server is needed |
| Language model (Claude API) | About €5 to €10 a month, plus €30 to €50 once for the first full scan | About €10 to €20 a month | About €20 to €50 a month |
| Email | Not needed | €0 via IPK's mail server, or a free tier of an email service | €0 |
| **Running costs per year** | **About €60 to €240** | **About €120 to €360** | **About €240 to €840** |
| Build effort | 15 to 25 developer days, plus 5 to 10 expert days | 15 to 25 developer days | 10 to 30 developer days per feature |
| Build effort in euros (at €500 to €700 per day) | About €10,000 to €24,500 | About €7,500 to €17,500 | About €5,000 to €21,000 per feature |
| Ongoing staff time | 1 to 2 hours a week | 2 to 4 hours a week | 2 to 4 hours a week |

The day rate of €500 to €700 is an assumption based on typical external developer rates; if an IPK bioinformatician builds it, use internal rates instead. Hosting prices are approximate and change often, so check them before budgeting.

**How the language model estimate was calculated.** The estimate assumes 150 sources, so it is generous for V1's 30 to 50.

- About 20 changed pages a day are screened by Claude Haiku 5.5 (about 10,000 tokens in, 300 out each): under $1 a month.
- About 5 genuinely new calls a day are extracted by Claude Sonnet 5.5 (about 20,000 tokens in, 1,500 out each): about $8 a month, or half that with the Batch API.
- List prices per million tokens: [Haiku 5.5](https://platform.claude.com/docs/en/models/haiku-5-5/overview) $0.10 in and $0.50 out for prompts up to 100,000 tokens; [Sonnet 5.5](https://platform.claude.com/docs/en/models/sonnet-5-5/overview) $2 in and $10 out; Batch API 50% off. Dollar amounts were converted at roughly €0.90 per dollar and rounded up.

## Risks and mitigations

The main risks are stale sources and wrong facts, not the technology; both are handled by monitoring and a short weekly review.

| Risk | Effect | Mitigation |
| --- | --- | --- |
| Funder websites change or move | Sources go silent and calls are missed | Health report of failed and unchanged sources; fix broken ones in the weekly review |
| The model gets a date or eligibility rule wrong | A researcher misses a deadline or applies when ineligible | Always link the original call; store the evidence text; human review before entries count as checked |
| Relevant calls hidden in broad programmes | The tool misses calls that matter most | Watch topic-level pages, not just funder homepages; ask researchers to report misses |
| Too many irrelevant entries | Users stop trusting the list | Relevance score with a cut-off; reviewer rejections teach the model |
| Legal and copyright | Complaints from funders or database owners | Use funders' own pages only, never ELFI or other paid databases; write own summaries; respect robots.txt |
| No long-term owner | The tool decays once the builder moves on | Agree an owner (likely the funding office) before V1 starts; keep the code simple and documented |

## Open questions and next steps

**Open questions**

- Does IPK already subscribe to ELFI or a similar service? If so, the tool should add focus, not duplicate it.
- Who owns the tool long term: the funding office, the bioinformatics group or IT?
- Where should it run: an IPK server or an EU cloud host?
- Is the tool for IPK staff only, or public?
- Who builds it, and from which budget?

**Next steps**

- [ ] Discuss the plan with the third-party funding office and two or three group leaders.
- [ ] Draft the first 30 to 50 sources with them.
- [ ] Agree hosting and ownership with IPK IT.
- [ ] Build V1 and run it alongside current practice for two months.
- [ ] Review against the success criteria and decide on V2.

## Prototype decisions (November 2026)

These apply to the demo prototype built in this repository, ahead of V1.

- **Hosting:** public GitHub repository `plant-funding-monitor`. GitHub Actions runs the daily job and saves the updated SQLite database back to the repository. GitHub Pages serves the search page, using Datasette Lite or a generated static page.
- **Sources:** `sources.csv` holds 12 demo sources with columns `id, funder, programme, url, access_method, content_selector, scope, check_frequency, status, notes`. Rows with status `needs_url` need their listing page or feed found before use. The pipeline keeps its own state (content hash, last checked, last changed) in the database, not in the CSV.
- **API key:** stored as the GitHub Actions secret `ANTHROPIC_API_KEY`. For local runs it lives in a `.env` file in the project folder (ignored by git). Never set it globally on the machine, and never print or commit it. The key belongs to the Console workspace `plant-funding-monitor` (monthly spend limit $10) and expires around 5 Feb 2027.
- **Models:** `claude-haiku-5-5` for screening and `claude-sonnet-5-5` for extraction. Keep each prompt under 100,000 tokens, where Haiku 5.5's lower price tier applies.
- **Screening scope:** broad, covering anything plant-related, including applied agronomy, plus calls open to any field.
- **Page cleaning:** strip navigation, menus and footers before sending text to the model. DFG pages, for example, are mostly menus with the content at the bottom.
- **Page conversion:** when turning web pages into text for the model, convert HTML to Markdown so links are kept.
- **Personal data:** leave funder contact persons' names, emails and phone numbers out of what is sent to the model and out of the results table.
- **Contacts:** only functional contacts (organisation, unit, team mailbox) are stored, never named individuals.
- **Test cases for checking results:** the BLE call on innovative plant protection measures (sketches due 14 Jan 2027) should be found; the DFG French-German call ANR-DFG 2027 NLE should be found; the DFG Japanese-German call on next-generation energy technologies (JST-DFG 2026) should be rejected as irrelevant.
- **Eligibility traps:** EMBO Installation Grants are not open to institutions in Germany; some VolkswagenStiftung funding is restricted to Lower Saxony. Both should show as not eligible for IPK.
- **Later:** switch GitHub Actions from the stored API key to identity federation once the pipeline works.
