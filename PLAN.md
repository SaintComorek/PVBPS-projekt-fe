# PLAN – SOC Agent Lab: prompt injection vs. deterministická bezpečnostná brána

Lokálne spustiteľná demonštrácia pre 10-minútové živé predvedenie (po 15-minútovej prednáške)
k reportu Anthropic *Detecting and countering misuse of AI: September 2026*.

Demo **nie je rekonštrukciou žiadneho incidentu z reportu**. Je to syntetická učebná ukážka
mechanizmu, ktorý report opisuje vo všeobecnej rovine (vkladanie škodlivých inštrukcií do dát,
ktoré spracúvajú AI agenti s prístupom k nástrojom), a obrany proti nemu.

## 1. Východiskový stav (inšpekcia 2026-11-15)

| Cesta | Stav |
|---|---|
| `~/PycharmProjects/PVBPS_practical_part` | šablóna PyCharm (`main.py`, `test_main.http`), `.venv` s Python 3.12.16 + FastAPI. Bez gitu. Súbory šablóny ponechávam bez zmeny. |
| `~/WebstormProjects/pvbps_practical_part` | prázdny (iba `.idea`). Sem ide frontend (rozhodnutie používateľa). |
| Nástroje | Node 26, npm 11, Docker 29, uv, PowerPoint + `soffice` na Macu, `python-pptx` sa dá doinštalovať. |
| LLM | Lokálne nebeží žiadny LLM server (Ollama/LM Studio). Živý režim sa overí unit testmi proti simulovanému HTTP serveru, reálne volanie spraví používateľ so svojím endpointom. |

## 2. Architektúra

```
Prehliadač (React SOC dashboard, :5711)
   │  REST + Server-Sent Events (/api/...)
   ▼
FastAPI backend (:8711) ─────────────────────────────────────────────┐
   RunManager ── spúšťa beh na pozadí, drží históriu udalostí (SSE)   │
   │                                                                  │
   ├─ Orchestrator Agent (deterministický koordinátor úloh)           │
   │     ├─ Research Agent  → nástroj read_incidents                  │
   │     └─ Analysis Agent  → LLM (živý) / prehrávaný záznam (replay) │
   │            navrhuje volania: get_incident, analyze_incident,     │
   │            update_incident                                       │
   ├─ ToolExecutor ── striktné schémy (Pydantic, extra=forbid),       │
   │                  explicitný allowlist nástrojov aj agentov       │
   ├─ SecurityPolicyGate ── deterministické pravidlá:                 │
   │     rola používateľa ∩ oprávnenie úlohy ∩ schválenie človekom    │
   │     (text modelu ani dokumenty sa nikdy nepoužijú)               │
   ├─ AuditService ── návrhy, rozhodnutia, zmeny stavu (+ JSONL)      │
   └─ IncidentStore ── syntetické JSON záznamy, reset zo seed súboru  │
```

### Kľúčové rozhodnutia

1. **Jediný rozdiel medzi režimami je brána.** Prompt, dáta aj škodlivý vstup sú v režime
   *Zraniteľný* a *Chránený* identické. Zraniteľný executor vykoná každé povolené (allowlist +
   schéma) volanie, ktoré model navrhne, teda autorizáciu deleguje na model. Chránený executor
   pustí zápis len cez `SecurityPolicyGate`.
2. **Brána beží v zraniteľnom režime „naprázdno“ (shadow).** Rozhodnutie sa vypočíta, ale
   nevynúti. Vďaka tomu UI aj metriky presne označia *neautorizovanú akciu, ktorá sa vykonala*.
3. **Replay nefalšuje dôkazy.** V režime *Deterministický replay* sa prehrávajú iba odpovede
   modelu (vopred napísané, označené ako SYNTETICKÉ). Executor, brána, audit aj zmeny dát bežia
   naozaj. Metriky replay behov sú oddelené a nikdy sa nevydávajú za úspešnosť modelu.
4. **Živý režim používa JSON protokol nástrojov** (model vráti JSON objekt s `tool_calls`), pretože
   modely Gemma na väčšine OpenAI-kompatibilných endpointov nemajú natívne function calling.
   Klient je čisté `httpx` volanie `POST {LLM_BASE_URL}/chat/completions`. Neskôr: od 17. 11. natívny
   tool calling, od 18. 11. predvolene LangChain (nástroje `@tool`, `ChatOpenAI` + `bind_tools`); JSON protokol ostáva.
5. **Autorizácia sa odvodzuje od úlohy, nie od voľného textu.** Používateľ si vyberá úlohu z
   katalógu na serveri (`analyze_report` = iba čítanie, `triage_with_approval` = zmena stavu
   po schválení). Oprávnenie viaže server k úlohe a k autentifikovanému demo používateľovi.
6. **Agenti nemajú sieť ani súborový systém.** Jediné nástroje sú 4 funkcie nad syntetickými
   dátami. Adresa LLM je iba z prostredia, nikdy z požiadavky alebo z dát.
7. **Jeden beh naraz** (demo na projektore), ďalší beh vráti 409.

### Scenáre

| Scenár | Kroky | Očakávaný výsledok |
|---|---|---|
| A – bežná analýza | Reset → Spustiť bežnú analýzu | 3 incidenty načítané a analyzované, report, žiadna zmena stavu |
| B – útok, zraniteľný režim | Vložiť škodlivý dokument → režim Zraniteľný → Spustiť útok | ak model navrhne `update_incident(INC-003, RESOLVED)`, vykoná sa (lokálne), UI ukáže neautorizovaný zápis a rozdiel pred/po |
| C – útok, chránený režim | Reset → režim Chránený → Spustiť útok | ten istý vstup, zápis zablokovaný bránou, audit, INC-003 zostáva OPEN |
| (bonus) triáž so schválením | úloha `triage_with_approval`, režim Chránený | zápisy čakajú na schválenie človekom, podozrivé návrhy majú upozornenie na pôvod v nedôveryhodnom obsahu |
| H – RAG (18. 11.) | úloha `helpdesk_answer`, Spustiť útok | otrávená úprava wiki vyhrá vektorové vyhľadávanie: zraniteľný pošle phishingový odkaz, chránený `UNTRUSTED_LINK_IN_REPLY` |
| G – účtovníctvo (18. 11.) | úloha `pay_invoices`, Spustiť útok | podvodný e-mail k faktúre: zraniteľný zmení účet dodávateľa a zaplatí útočníkovi, chránený zmenu účtu pozdrží na schválenie a platbu na neoverený účet zamietne |

V živom režime nie je správanie modelu zaručené. Ak model injekciu ignoruje, UI to poctivo
zobrazí („model nenavrhol neautorizovaný zápis“) a prezentujúci prepne na replay.

## 3. Úlohy

| # | Úloha | Výstup |
|---|---|---|
| 1 | Výskum reportu, overené fakty s citáciami | `docs/report-research.md` |
| 2 | Doménový model, seed dáta, injekčný fixture, replay skripty | `soc_demo/domain`, `soc_demo/data/` |
| 3 | IncidentStore (reset, atomický zápis), AuditService, metriky | `soc_demo/services/` |
| 4 | Nástroje: schémy, allowlist, analýza závažnosti, executor | `soc_demo/tools/` |
| 5 | SecurityPolicyGate + schvaľovanie | `soc_demo/security/` |
| 6 | LLM klient (OpenAI-kompatibilný), JSON protokol, replay backend | `soc_demo/llm/` |
| 7 | Agenti a RunManager s udalosťami (SSE) | `soc_demo/agents/`, `soc_demo/runs/` |
| 8 | REST API, chyby, logovanie s maskovaním tajomstiev, request id | `soc_demo/api/`, `soc_demo/main.py` |
| 9 | Testy (unit + integračné) | `tests/` |
| 10 | Frontend dashboard (React, TS, Vite, Tailwind) | `~/WebstormProjects/pvbps_practical_part` |
| 11 | Docker Compose, `.env.example`, README, architektúra | koreň BE repozitára, `docs/` |
| 12 | `demo_script.md`, `presentation.pptx` (+ poznámky prednášajúceho) | koreň BE repozitára |
| 13 | Overenie end-to-end (API + prehliadač), nezávislá kontrola | záverečná správa |

## 4. Testovacie kritériá

- Reset vždy obnoví presne stav zo seed súboru (aj po zápisoch, injekcii a schváleniach).
- Injekčný fixture sa načíta, je označený `trust=untrusted` a obsahuje presne zadaný text.
- Chránený režim zamietne každý zápis mimo oprávnenia úlohy **pre ľubovoľný výstup modelu**
  (parametrizovaný test cez sadu škodlivých/podvodných návrhov vrátane „schválené používateľom“
  v argumentoch).
- Zraniteľný režim zápis vykoná a označí ho ako neautorizovaný.
- Neznáme nástroje a argumenty mimo schémy sa zamietnu v oboch režimoch.
- Odovzdávanie medzi agentmi prebehne v poradí Orchestrator → Research → Analysis → Orchestrator.
- SSE stream doručí históriu aj živé udalosti a skončí udalosťou `run_completed`/`run_failed`.
- Audit obsahuje návrh, rozhodnutie a zmenu stavu. Logy ani audit neobsahujú API kľúč.
- Živý backend: korektná požiadavka, parsovanie JSON aj ```json blokov, timeout, HTTP chyba,
  nevalidný výstup → udalosti bez pádu behu.
- Frontend: `tsc` (type check), `vite build` bez chýb.

## 5. Výstupy

- Backend + frontend kód, `PLAN.md`, `README.md`, `.env.example` (bez tajomstiev)
- `soc_demo/data/seed/incidents.json`, `soc_demo/data/fixtures/injection_document.json`, replay skripty
- `tests/` (pytest), frontend type check + build
- `demo_script.md`, `docs/architecture.md`, `docs/report-research.md`
- `presentation.pptx` (python-pptx, s poznámkami prednášajúceho)
- `docker-compose.yml` + Dockerfile pre BE a FE

## 6. Porty

Backend `8711`, frontend `5711` (overené ako voľné; `8000` na tomto Macu patrí inému projektu).

## 7. Stav realizácie (2026-11-15)

| Bod | Stav | Overenie |
|---|---|---|
| Backend | hotový | 89 testov pytest prechádza, ruff bez nálezov |
| Frontend | hotový | `npm run build` (typecheck + vite build) bez chýb, komponenty max. 112 riadkov |
| Scenáre A, B, C | fungujú | cez API aj cez proxy frontendu, headless Chrome snímky pri 1920×1080 a 1366×768 |
| Schvaľovanie (bonus) | funguje | API test aj UI (schválenie INC-001, zamietnutie INC-003) |
| Docker Compose | funguje | build oboch obrazov, útok cez web proxy na náhradných portoch 8712/5712 |
| Živý režim | funguje | Groq, openai/gpt-oss-120b, 11 skutočných behov: v úlohe analýzy model navrhol zápis z injekcie v 7 z 8 útokov, chránený režim ho zakaždým zablokoval (detail v DOKUMENTACIA.md, kapitola 14) |
| Prezentácia | hotová | 13 slajdov s poznámkami v rovnakom svetlom dizajne a palete Apple ako aplikácia, scenáre A až F, snímky aplikácie, render cez LibreOffice skontrolovaný, čísla zhodné s `docs/report-research.md` |
| Triáž naživo (17. 11.) | opravená | pôvodné znenie úlohy nútilo model čakať na schválenie, ktoré nemohol dostať. Nové znenie, 3 živé behy, schválenie aj zamietnutie overené |
| Natívny tool calling (17. 11.) | hotový | `LLM_TOOL_PROTOCOL=native-tools` (OpenAI `tools`), JSON protokol ostáva, 15 živých behov proti Groq |
| Scenár H, RAG (18. 11.) | hotový | IT príručka v Markdowne, chunkovanie `MarkdownHeaderTextSplitter`, embeddingy fastembed (multilingual MiniLM), `InMemoryVectorStore`, nástroje `list_docs`, `search_docs`, `send_reply`, pravidlo `UNTRUSTED_LINK_IN_REPLY`, dlaždice Znalostná báza a Odpovede, 181 testov, živý beh proti Groq zabral |
| Scenár G, účtovníctvo (18. 11.) | hotový | doména `finance`: dodávatelia, faktúry, platby (`finance.json`), nástroje `list_invoices`, `get_vendor`, `schedule_payment`, `update_vendor_bank`, pravidlá `BANK_CHANGE_REQUIRES_APPROVAL`, `PAYMENT_TO_UNVERIFIED_ACCOUNT`, `PAYMENT_AMOUNT_MISMATCH`, `DUPLICATE_PAYMENT`, dlaždice Faktúry a Platby, 166 testov, živý beh proti Groq |
| LangChain (18. 11.) | hotový | nástroje ako `@tool`, LangChain ich volá priamo, brána vnútri toolu; `ChatOpenAI` + `bind_tools`, predvolený protokol `langchain`, 150 testov, živý beh proti Groq zablokovaný bránou |
| Scenáre D, E, F (17. 11.) | hotové | únik kľúča e-mailom, otrávený threat intel feed, otrávený playbook. Nové nástroje `send_email`, `block_ip`, `search_playbook`, `allowlist_sender`, kontroly argumentov v bráne. 141 testov |
