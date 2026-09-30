# Why this matters: the problem, with evidence

Saakshi is built for one situation that repeats across India and the rest of the developing world: **people doing development work in places with bad or no connectivity have to prove, with photos, what they did.** Money, wages and trust depend on that proof. Each claim below links to its source.

## 1. The network fails exactly where the work happens

* **2.2 billion people are still offline** in 2025, and the gap is rural: **85% of people in urban areas use the internet versus 58% in rural areas**. 96% of those offline live in low- and middle-income countries. ([ITU, Facts and Figures 2025](https://www.itu.int/en/mediacentre/Pages/PR-2025-11-17-Facts-and-Figures.aspx))
* **350 million people live in areas with no mobile internet network at all**, and that coverage gap "predominantly exists in rural, poor and sparsely populated areas – often less developed, landlocked, or small island developing states". ([GSMA, State of Mobile Internet Connectivity 2024](https://www.gsma.com/newsroom/press-release/new-gsma-report-shows-mobile-internet-connectivity-continues-to-grow-globally-but-barriers-for-3-45-billion-unconnected-people-remain/))
* In India, **83.3% of rural households** report internet access ([MoSPI Comprehensive Modular Survey: Telecom 2025](https://www.pib.gov.in/PressReleasePage.aspx?PRID=2132330&reg=48&lang=2)). Access at home is not connectivity at the worksite: the embankment, the pond, the forest plot, the island.
* Disasters take the network down when it is needed most. Cyclone Remal (May 2024) put **over 10,000 mobile towers out of service** in coastal Bangladesh, across the same delta as the Indian Sundarbans. ([The Daily Star, 27 May 2024](https://www.thedailystar.net/business/news/cyclone-disrupts-10000-telecom-towers-millions-out-service-3620101))

**What Saakshi does about it:** capture, search, the Ask assistant and the sync decisions run on the device, with the network off. Vectors (a few KB) travel before photos, urgent reports first, and on a 2G link routine photos wait or go compressed. See the simulated field day in [BENCHMARKS.md](BENCHMARKS.md): in a place that is offline 65% of the time, a cloud app answers 37% of searches; Saakshi answers all of them.

## 2. Photo evidence already decides wages and funds, and it breaks

India's rural jobs scheme (MGNREGA) requires **two time-stamped, geotagged photographs of workers a day** through the NMMS app.

* A parliamentary committee found that "the actual success of this app depended upon various extraneous variables like the availability of smartphones, **proper internet connectivity** and the mandatory presence of MGNREGA workers at both the specified times", and asked the government to review it. ([Business Standard, 27 July 2023](https://www.business-standard.com/india-news/parl-panel-urges-govt-to-review-use-of-nmms-app-for-attendance-issues-123072701158_1.html))
* Reporting in 2026 describes attendance "marked using **irrelevant photographs, images of fields, trees, or loosely assembled groups**, without any meaningful verification", and cases where **30–40% of workers could not get attendance registered**. ([The Wire, 20 April 2026](https://m.thewire.in/article/labour/nmms-didnt-end-corruption-in-mgnrega-it-changed-its-shape-and-locked-workers-out))

The same two failures, connectivity and unverifiable photos, apply to every NGO and CSR programme that reports with photos.

**What Saakshi does about it:** capture never depends on the network; every photo keeps its EXIF GPS/time and fingerprints (SHA-256, dHash) from the moment of capture; the cloud checks each photo against every earlier submission (reuse detection by hash, pHash and CLIP similarity), the project area and the project dates, and the verdict flows back to the officer's device.

## 3. Impact claims are under pressure to be verifiable

* India's CSR rules require **independent impact assessment** for companies with an average CSR obligation of ₹10 crore or more, for projects with outlays of ₹1 crore or more. ([Companies (CSR Policy) Amendment Rules 2021, summary by ClearTax](https://cleartax.in/s/csr-amendment-rules-2021))
* Companies spent **₹34,908.75 crore on CSR in FY 2023-24** (27,188 companies, National CSR Portal data). ([The CSR Journal, 20 June 2025](https://thecsrjournal.in/india-inc-spent-rs-34908-75-crore-on-csr-in-fy24-national-csr-portal/))
* When claims are not verified on the ground they fail: a joint investigation found **more than 90% of rainforest offset credits** from the largest standard were likely "phantom credits". ([The Guardian, Die Zeit and SourceMaterial, via Carbon Brief, 19 January 2023](https://www.carbonbrief.org/daily-brief/revealed-more-than-90-of-rainforest-carbon-offsets-by-biggest-provider-are-worthless-analysis-shows/))
* Planting is not survival: of about **1,000 ha of mangroves replanted in Sri Lanka after the 2004 tsunami, only about 200 ha succeeded, and at 9 of 23 sites not a single plant survived**. ([Kodikara et al., Restoration Ecology 2017](https://onlinelibrary.wiley.com/doi/abs/10.1111/rec.12492), summarised by [Anthropocene Magazine](https://www.anthropocenemagazine.org/2017/03/mangrove-restoration-problems/))

**What Saakshi does about it:** evidence is organised by site over time. Each site has a timeline (photos, status reports, conflicts, HQ checks), before/after pairs are found automatically, and conflicting reports from different officers are surfaced instead of silently overwritten. A funder sees "planted in March, 60% alive in September, verified", not a single photo from planting day.

## 4. Photos of people are personal data

* India's **Digital Personal Data Protection Rules** were notified on 14 November 2025, with consent notices, security safeguards and verifiable parental consent for children's data phased in over 18 months. ([India Briefing](https://www.india-briefing.com/news/dpdp-rules-2025-india-data-protection-law-compliance-40769.html/); [PIB notification](https://static.pib.gov.in/WriteReadData/specificdocs/documents/2025/nov/doc20251117695301.pdf))
* NGO field photos routinely show beneficiaries, including children.

**What Saakshi does about it:** faces are detected on the device. Without recorded consent, only a face-blurred copy leaves the device (or the photo is held for approval, the officer's choice); private items never leave. Public outputs are blurred again in the cloud (`e_blur_faces`). In the simulated field day, a cloud app uploads about 37 unblurred photos of people per team per day; Saakshi uploads none.

## Who uses it

| Person | Today | With Saakshi |
|---|---|---|
| Field officer in a dead zone | Photos pile up in the gallery or WhatsApp; no way to check what a colleague already reported about this site | Captures and searches offline, sees colleagues' evidence for their region, asks "what is the latest at Pond A?" and gets an answer with sources |
| Programme manager | Chases photos, cannot tell new from reused, learns about a breached embankment days later | Urgent reports arrive first, each photo arrives with an integrity score, conflicts are flagged |
| Funder / CSR team | A report with a few photos and no way to audit them | A report where every photo traces back to its original capture, place and time |
