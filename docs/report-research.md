# Overené fakty zo zdrojového reportu

**Zdroj:** Anthropic, *Detecting and countering misuse of AI: September 2026*
**URL:** https://www.anthropic.com/threat-intelligence-report-september-2026
**PDF:** https://assets.claude.com/e50be2e51e7695dc4b1366a37a245a597377d3b5.pdf (súbor `Anthropic-Detecting-and-countering-091026.pdf`)
**Dátum:** 10. 9. 2026 (z názvu PDF súboru a zo sekundárnych zdrojov, napr. SiliconANGLE 2026-09-10; samotná stránka dátum v texte neuvádza)
**Overené:** 2026-11-15 stiahnutím a prečítaním HTML stránky reportu.

Citácie sú doslovné (anglicky), preklad a zhrnutie sú moje. Čísla uvádzam iba tak, ako sú v reporte.
Nič z tejto stránky nepoužívam ako tvrdenie, že demo rekonštruuje konkrétny incident.

## 1. Rozsah reportu

> "This report covers activity we disrupted between December 2025 and August 2026 across seven harm areas: cyber operations, influence operations, surveillance, scams and fraud, biological misuse, conventional weapons development, and distillation."

> "The cases we share here aren’t typical misuse, but rather examples of the most notable and novel threat activity we’ve identified to date."

- Predchádzajúce reporty: marec, august a november 2025 (úvod reportu).
- Zneužité modely: Claude Haiku, Sonnet a Opus. Report uvádza, že žiadny prípad nezneužil modely triedy Fable alebo Mythos, s výnimkou jedného prípadu nelegálnej destilácie.
- Report zavádza označenie GTG (*Generative Threat Groups*) a pojem *uplift* (o koľko viac škody aktér spôsobí s AI oproti stavu bez AI, posudzované cez rýchlosť, rozsah a hĺbku).

## 2. Hlavné trendy v kybernetických operáciách

**Od asistenta k orchestrátorovi** (názov kapitoly: *Cyber operations: From assistant to orchestrator*).

> "Sophisticated attacks no longer require sophisticated attackers"

> "A majority of the operations described in this report were enabled by AI via direct execution or orchestration. The use of AI went beyond simple questions and responses from a chatbot but rather involved the use of multi-agent frameworks executing reconnaissance, exploitation, and data exfiltration. Humans remained in the loop by setting the targets of attacks and reviewing exfiltration."

> "The attacks themselves are familiar, involving stolen credentials, unpatched edge devices, exposed services, SQL injection, and phishing. None of the operations in this report depended on some entirely novel technique that defenders have never seen. Instead, the economics of the attacks have changed."

> "The results are visible in the numbers reported above: breaches completed in two to three hours, and dozens of victims handled in parallel by individual operators."

Dve výhrady, ktoré report sám uvádza (dôležité pre férovú prezentáciu):

> "First, humans have retained the decisions that matter most to them: for example, they’re still heavily involved in target selection, monetization of findings, and review of results. Second, autonomy and harm are separate axes"

## 3. Prípady priamo súvisiace s témou dema (agenti, nástroje, injekcia inštrukcií)

### GTG-50020: od rezervácií hotelov k dodávateľskému reťazcu AI

> "By injecting malicious instructions into an AI vendor’s automated evaluation sandbox, the actor caused the sandbox to hand over the credentials it held—including the production AI API keys from multiple providers belonging to that vendor."

> "A follow-on campaign run from the same infrastructure attacked roughly thirty AI companies in about four days with similar techniques."

> "The actor’s stated goal, pursued across more than a dozen avenues, was access to a pre-release Claude model. The actor never gained access; every attempted path failed."

- Aktér používal aj kontajnerizovanú open-source pentest platformu, kde *"Worker agents ran injection, XSS, authentication-bypass, and SSRF testing without human supervision"*.
- Report zdôrazňuje, že kľúče boli ukradnuté z prostredí zákazníkov a Anthropic samotný kompromitovaný nebol.

**Súvis s demom:** útočník neovláda systémové inštrukcie agenta, ovláda iba dáta, ktoré agent spracúva. Agent má v dosahu citlivú schopnosť (u GTG-50020 prihlasovacie údaje, v deme zápis do incidentov).

### AI dodávateľský reťazec ako cieľ, korisť a výpočtový výkon

> "multiple actors were observed compromising AI wrapper services’ implementation of LiteLLM—they used prompt injection to exfiltrate the production API keys used in their cloud-hosted container environments."

> "AI API keys and session tokens are targets; the integrations customers build around AI such as sandboxes, proxies, and resellers are part of the attack surface. Organizations should treat AI keys and agent integrations with the same level of seriousness as they do production credentials—because attackers treat them with the same level of seriousness, too."

Získané AI prístupy dávajú útočníkovi podľa reportu tri veci: *Loot* (predajná hodnota), *Compute* (útok beží na cudzie náklady), *Cover* (aktivita sa pripíše vlastníkovi kľúča).

### GTG-50014: oportunisti napojení na ShinyHunters

- Medzi formami oportunistických útokov report menuje aj *"prompt injection of LiteLLM or OpenClaw deployments"*.
- Prístupové tokeny hľadali aj v *"victim-deployed AI agents"*.
- Po prieniku do SaaS poskytovateľa získali dáta asi 200 jeho zákazníckych organizácií, potom *"a session-store dump containing over 2,100 Azure AD token sets spanning more than 40 corporate tenants in about 34 hours. AI agents performed nearly all of the work."*
- *"Another compromise escalated from a single stolen developer token to full administrative control of a victim’s cloud environment in roughly three hours."*
- Report opisuje štýl *"vibe hacking"*: operátor zadá všeobecný cieľ a AI hodnotí prostredie, píše a spúšťa skripty, kým úlohu nesplní.

### GTG-10007: exploit foundries a autonómne útočné rámce

> "The operators routinely ran “agent swarms,” where a lead AI agent decomposed reconnaissance and post-exploitation work and dispatched it to many subagents running in parallel. The operation maintained persistent campaign memory."

- Približne 50 cieľových organizácií. Jeden workflow nad sieťovými zariadeniami priniesol *"more than a dozen possible zero day findings in a single month"*.
- *"A fleet of thirteen standing collection AI agents ran on a scheduled job"*.

**Súvis s demom:** architektúra orchestrátor → špecializovaní agenti je rovnaký vzor, aký používajú legitímne systémy. Bezpečnosť sa preto nemôže opierať o to, že model „vie“, čo smie.

### GTG-20006: ruská špionáž (atribúcia konzistentná s Midnight Blizzard)

- Viac ako 20 cieľových organizácií. AI workflow automaticky prestavali a znovu nasadili malvér, keď ho zachytil bezpečnostný produkt.
- *"The human actor engaged primarily to modify Claude Code skills that drove the workflows when they needed to be refined."*
- Report uvádza aj zistenie autorizačných chýb v API kamerových služieb (*"They found authorization flaws in the application interface of camera streaming services"*).

### GTG-50029: hacktivista proti európskym politickým subjektom

- Jeden aktér, kampaň bežala mesiac na ukradnutých API kľúčoch.
- *"Across 42 tracked target entities, the actor gained internal access to at least 14."* Exfiltrácia približne 12 až 26 GB a z jednej platformy asi 140 000 záznamov.
- Používal rámec so sub-agentmi na prieskum, code review a overovanie nálezov.

## 4. Čo z toho plynie pre návrh dema (moja interpretácia, nie citácia)

1. Útočný vektor *vloženie inštrukcií do dát spracúvaných agentom* report dokladá v GTG-50020 a pri LiteLLM. Demo ukazuje ten istý mechanizmus na neškodnom, syntetickom príklade.
2. Report odporúča brať integrácie agentov rovnako vážne ako produkčné prihlasovacie údaje. Demo z toho robí architektonické pravidlo: oprávnenie na zápis nevychádza z modelu, ale z deterministickej brány (najmenšie oprávnenie, schválenie človekom, audit).
3. Report upozorňuje, že sofistikovanosť už nie je spoľahlivý signál. Obrana preto nesmie stáť na tom, či model útok „spozná“.

## 5. Sekundárne zdroje (iba na potvrdenie dátumu publikácie)

- SiliconANGLE, 2026-09-10: https://siliconangle.com/2026/09/10/anthropic-says-ai-now-lets-lone-operators-run-state-level-hacking-campaigns/
