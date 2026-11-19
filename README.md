# SOC Agent Lab

Lokálna demonštrácia k reportu Anthropic *Detecting and countering misuse of AI: September 2026*
(https://www.anthropic.com/threat-intelligence-report-september-2026).

Dashboard bezpečnostného operačného centra ukazuje, ako spolupracujú AI agenti (Orchestrator, Research,
Analysis), ako môže škodlivá inštrukcia ukrytá v nedôveryhodných dátach incidentu priviesť agenta k návrhu
neautorizovaného volania nástroja a ako takýto zápis zastaví deterministická bezpečnostná brána na serveri.

> Demo nie je rekonštrukciou žiadneho incidentu z reportu. Všetky dáta sú syntetické (fiktívna firma,
> domény `.example`, dokumentačné IP adresy). Agenti nemajú prístup k sieti, súborom ani reálnym systémom.
> Overené fakty z reportu s citáciami sú v [docs/report-research.md](docs/report-research.md).

## Čo je kde

| Cesta | Obsah |
|---|---|
| `soc_demo/` | backend (FastAPI, Pydantic, LangChain): agenti, nástroje `@tool`, bezpečnostná brána, audit, živý model cez LangChain, SSE |
| `soc_demo/data/seed/incidents.json` | 3 syntetické incidenty (zdroj pre reset) |
| `soc_demo/data/seed/environment.json` | simulované prostredie: firemné domény, chránené siete, aktíva, firewall, allowlist, playbooky |
| `soc_demo/data/fixtures/*.json` | štyri simulované útoky (príloha, komentár v issue, threat intel feed, úprava wiki) |
| `soc_demo/data/replay/scripts.json` | syntetické odpovede modelu pre Deterministický replay |
| `tests/` | pytest: unit + integračné testy |
| `docs/` | architektúra, API kontrakt, výskum reportu |
| `DOKUMENTACIA.md` | kompletná technická dokumentácia |
| `PREZENTACIA.md` | návod na celé vystúpenie: prednáška slajd po slajde, demo, otázky a odpovede |
| `PLAN.md`, `demo_script.md`, `presentation.pptx` | plán, scenár živého dema (10 min), prezentácia (15 min) |
| `docs/screenshots/` | skutočné snímky aplikácie (záloha pre prezentáciu) |
| `~/WebstormProjects/pvbps_practical_part` | frontend (React, TypeScript, Vite, Tailwind) |
| `main.py`, `test_main.http` | pôvodná šablóna PyCharm, demo ich nepoužíva |

## Rýchly štart (lokálne)

Predpoklady: Python 3.11+, Node.js 20+.

```bash
# 1) Backend – port 8711
cd ~/PycharmProjects/PVBPS_practical_part
python3 -m venv .venv            # ak .venv ešte neexistuje
.venv/bin/pip install -e ".[dev]"
.venv/bin/uvicorn soc_demo.main:app --host 127.0.0.1 --port 8711

# 2) Frontend – port 5711 (druhý terminál)
cd ~/WebstormProjects/pvbps_practical_part
npm install
npm run dev

# 3) Prehliadač
open http://127.0.0.1:5711
```

Bez ďalšej konfigurácie funguje režim **Deterministický replay**. Kontrola backendu:
`curl http://127.0.0.1:8711/api/health`.

Scenár H (RAG) používa lokálny embedding model `paraphrase-multilingual-MiniLM-L12-v2` (fastembed, asi 220 MB).
Pri prvom štarte backendu sa stiahne z verejného HuggingFace do `var/models` a potom beží offline. Pred
prezentáciou bez internetu preto spustite backend aspoň raz s pripojením (v logu `knowledge_base_ready`).

## Živý AI režim (Groq, gpt-oss-120b)

Backend volá ľubovoľné OpenAI-kompatibilné Chat Completions API (`POST {LLM_BASE_URL}/chat/completions`),
takže nepotrebuje SDK `groq`. Nastavenia bez tajomstiev sú pripravené v súbore `settings.env`:
Groq (`https://api.groq.com/openai/v1`), model `openai/gpt-oss-120b`, `reasoning_effort=medium`, JSON režim
a opakovanie pri limite požiadaviek.

1. Do súboru `.env` (nie do `.env.example`) vložte iba kľúč: `GROQ_API_KEY=gsk_...` (funguje aj `LLM_API_KEY`).
2. Reštartujte backend. `GET /api/health` musí ukázať `"configured": true` a `"api_key_configured": true`
   (kľúč sa nikdy nezobrazuje).
3. V dashboarde prepnite na **Živá AI**.

Hodnoty v `.env` majú prednosť pred `settings.env`, takže iný endpoint (napríklad lokálnu Ollamu
`LLM_BASE_URL=http://localhost:11434/v1`, `LLM_MODEL=gemma3:12b`) nastavíte v `.env` bez zásahu do `settings.env`.

Nástroje sú LangChain `@tool`. Model ich dostane cez `bind_tools`, LangChain ich zavolá priamo a každé volanie
ide cez bezpečnostnú bránu. Pre modely bez function callingu je `LLM_TOOL_PROTOCOL=json-tools`. Ak endpoint vracia
uvažovanie modelu (gpt-oss na Groq ho vracia), časová os ho ukáže pri výstupe modelu.
Správanie modelu nie je zaručené: model môže injekciu ignorovať, odmietnuť alebo vrátiť nevalidný výstup.
Všetko sa zobrazí tak, ako sa naozaj stalo, a metriky živých behov sú oddelené od syntetických.

## Docker Compose

Backend a frontend sú dve repozitáriá. Naklonujte ich vedľa seba, frontend do priečinka `frontend`:

```bash
mkdir soc-agent-lab && cd soc-agent-lab
git clone https://github.com/SaintComorek/PVBPS-projekt-be.git backend
git clone <url-frontendu> frontend
cd backend
docker compose up --build            # web na http://localhost:5711, API na :8711
# iné porty: API_PORT=8712 WEB_PORT=5712 docker compose up --build
# frontend inde: FRONTEND_DIR=/cesta/k/fe docker compose up --build
```

Pre živý režim vytvorte vedľa `docker-compose.yml` súbor `.env` s jediným riadkom `GROQ_API_KEY=gsk_...`.
Bez kľúča funguje režim Replay. Obraz backendu má závislosti presne podľa `uv.lock` a embedding model pre RAG
sa stiahne už pri builde, takže kontajner pri štarte nič nesťahuje. Pre Ollamu bežiacu na hostiteľovi použite
`LLM_BASE_URL=http://host.docker.internal:11434/v1`.

## Scenáre

| Scenár | Kroky v UI | Výsledok |
|---|---|---|
| A – bežná analýza | Reset dema → Spustiť bežnú analýzu | 3 incidenty analyzované, report, žiadna zmena stavu |
| B – útok, zraniteľný režim | Zraniteľný → Spustiť útok | ak model navrhne zápis, vykoná sa: INC-003 OPEN → RESOLVED, označené ako neautorizované |
| C – útok, chránený režim | Reset dema → Chránený → Spustiť útok | brána zápis zamietne, audit ho zaznamená, INC-003 zostáva OPEN |
| bonus – triáž | úloha „Triáž so schválením zmien“, Chránený | zápisy čakajú na schválenie, podozrivý návrh má upozornenie na pôvod |
| D – únik kľúča e-mailom | úloha „Notifikácia vedúcej SOC“ → Spustiť útok | zraniteľný: e-mail s kľúčom na `acme-audit.example`, chránený: `EXTERNAL_RECIPIENT_BLOCKED` |
| E – otrávený threat intel | úloha „Zadržanie hrozby“ → Spustiť útok | zraniteľný: firewall odreže vlastnú poštovú bránu, chránený: `PROTECTED_ASSET` |
| F – otrávený playbook | úloha „Postup podľa playbooku“ → Spustiť útok | zraniteľný: doména útočníka na allowliste a INC-002 uzavretý, chránený: allowlist zamietnutý, uzavretie čaká na schválenie |
| H – otrávená znalostná báza (RAG) | úloha „Odpoveď zamestnancovi (RAG)“ → Spustiť útok | skrytý komentár v úprave wiki vyhrá vektorové vyhľadávanie; zraniteľný: zamestnanec dostane phishingový odkaz na reset hesla, chránený: `UNTRUSTED_LINK_IN_REPLY` |
| G – podvodná zmena účtu dodávateľa (účtovníctvo) | úloha „Úhrada faktúr dodávateľov“ → Spustiť útok | zraniteľný: účet dodávateľa zmenený bez overenia a 4 870 € na účet útočníka, chránený: zmena účtu čaká na schválenie, platba `PAYMENT_TO_UNVERIFIED_ACCOUNT` |

Podrobný scenár prezentácie s časovaním: [demo_script.md](demo_script.md).

## Testy

```bash
.venv/bin/python -m pytest            # backend (150 testov)
cd ~/WebstormProjects/pvbps_practical_part && npm run typecheck && npm run build
```

Testy pokrývajú odovzdávanie medzi agentmi, operácie nad incidentmi, načítanie injekčného fixture,
autorizáciu (vrátane parametrizovaného testu, že chránený režim zamietne zápis pri ľubovoľnom výstupe modelu),
reset, audit, SSE stream, schvaľovanie, LLM klienta proti testovaciemu dvojníkovi servera a maskovanie tajomstiev.

## Bezpečnostné vlastnosti

- Explicitný allowlist 4 nástrojov a striktné schémy argumentov (`extra="forbid"`, vzor `INC-\d{3}`, enum stavov).
- Autorizácia zápisu: rola používateľa ∩ oprávnenie úlohy zo serverového katalógu ∩ schválenie človekom.
  Text modelu ani obsah dokumentov sa do rozhodnutia nedostanú.
- V chránenom režime každý zápis vyžaduje výslovné schválenie viazané na odtlačok konkrétneho volania.
- Zraniteľný režim mení iba lokálne syntetické dáta v `var/incidents.json`. Reset ich obnoví zo seed súboru.
- Adresa LLM je len z prostredia. Logy, audit a chybové hlásenia maskujú API kľúč.

## Prezentácia

`presentation.pptx` (13 slajdov s poznámkami) sa dá znova vygenerovať bez zásahu do `.venv`:

```bash
uv run --no-project --with python-pptx python tools/build_presentation.py
```

## API

Kontrakt pre frontend: [docs/api.md](docs/api.md). Interaktívna dokumentácia: http://127.0.0.1:8711/docs.

## Obmedzenia

- Demo používateľ je jeden a pevne daný (v produkcii by ho určil SSO/OIDC token).
- Orchestrator a Research Agent sú deterministickí koordinátori. LLM rozhoduje iba v Analysis Agentovi,
  čo je presne miesto, kde sa stretávajú nedôveryhodné dáta a nástroje.
- Výsledky živého režimu závisia od konkrétneho modelu a nastavení. Malý počet behov nie je štatistika.
- Stav je v pamäti procesu a v `var/`. Reštart backendu obnoví seed.
