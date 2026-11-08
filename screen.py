#!/usr/bin/env python3
"""Abnormal Movers screen (IREN capital markets desk).

Usage:
  python3 screen.py run --out OUT [--notes-in notes.json]
  python3 screen.py merge-notes --out OUT --new new_notes.json

`run` builds the universe, screens it, and writes into OUT:
  movers.json      rows for every flagged name and every peer (see FIELDS below)
  markets.json     one row per market with counts, state and run time
  sparks.json      30 sessions of closes for every row in movers.json
  needs_notes.json flagged names that still need a one-line note, with headlines
  notes.json       the notes cache (input cache pruned to 7 days)
`merge-notes` writes notes from new_notes.json ({symbol: note}) into movers.json
and notes.json.

Rules (agreed 27 Oct 2026):
  universe   S&P 500, NASDAQ-listed >= US$1bn, ASX ~300 (top of market by cap),
             Nikkei 225, KOSPI 200 (top KOSPI names by cap)
  stage      trailing revenue >= US$50m, else excluded (pre-revenue punt)
  tier 1     market cap >= US$10bn and |z| >= 3, z = today's log return against
             the name's own 200-session return distribution
  tier 2     |5-session move| >= 30%
  tier 3     |today's move| >= 10%
  peers      crypto miners / AI-infra names always shown in their own strip
Stdlib only. Python 3.8+.
"""
import argparse, concurrent.futures as cf, datetime as dt, html, http.cookiejar, json, math, os, re, sys, time
import urllib.parse, urllib.request
import xml.etree.ElementTree as ET
from html.parser import HTMLParser

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36"

DAY_PCT, WEEK_PCT, Z_MIN = 10.0, 30.0, 3.0
LARGE_CAP_USD, REV_MIN_USD = 10e9, 50e6
NASDAQ_MIN_CAP_USD, ASX_MIN_CAP_AUD, KR_MIN_CAP_KRW, JP_MIN_CAP_JPY = 1e9, 350e6, 1_000e9, 150e9
CAND_DAY, CAND_LARGE_DAY, CAND_MA50 = 5.0, 2.0, 20.0
PEERS = ["IREN", "CIFR", "CORZ", "APLD", "WULF", "HUT", "RIOT", "MARA", "CLSK", "BTDR", "NBIS", "CRWV", "GLXY"]
MARKETS = ["S&P 500", "NASDAQ", "ASX", "Nikkei 225", "KOSPI"]
NOTE_TTL_DAYS = 7
LOG = []


def log(msg):
    line = "%s %s" % (dt.datetime.utcnow().strftime("%H:%M:%S"), msg)
    LOG.append(line)
    print(line, flush=True)


def now_iso():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------- HTTP
def get(url, opener=None, headers=None, timeout=40, data=None):
    h = {"User-Agent": UA, "Accept": "*/*", "Accept-Language": "en-US,en;q=0.9"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h, data=data)
    op = opener or urllib.request.build_opener()
    with op.open(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def get_retry(url, tries=3, **kw):
    err = None
    for i in range(tries):
        try:
            return get(url, **kw)
        except Exception as e:
            err = e
            time.sleep(1.5 + 2 * i)
    raise err


class Yahoo:
    def __init__(self):
        self.op, self.crumb = None, None
        for attempt in range(4):
            try:
                cj = http.cookiejar.CookieJar()
                op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
                try:
                    get("https://fc.yahoo.com", op, timeout=20)
                except Exception:
                    pass
                crumb = get("https://query2.finance.yahoo.com/v1/test/getcrumb", op, timeout=20).strip()
                if crumb and len(crumb) < 30 and "{" not in crumb and " " not in crumb:
                    self.op, self.crumb = op, crumb
                    log("yahoo crumb ok")
                    return
            except Exception as e:
                log("crumb attempt %d failed: %s" % (attempt, str(e)[:80]))
            time.sleep(3 + attempt * 4)
        log("WARNING: no yahoo crumb; quote/quoteSummary calls will fail, chart calls still work")

    def json(self, url, tries=3):
        if self.crumb:
            url += ("&" if "?" in url else "?") + "crumb=" + urllib.parse.quote(self.crumb)
        err = None
        for i in range(tries):
            try:
                return json.loads(get(url, self.op))
            except Exception as e:
                err = e
                time.sleep(1.5 + 2 * i)
        raise err

    def chart(self, sym, rng="1y"):
        url = "https://query1.finance.yahoo.com/v8/finance/chart/%s?range=%s&interval=1d&includePrePost=false" % (urllib.parse.quote(sym), rng)
        try:
            j = json.loads(get_retry(url))
        except Exception:
            j = json.loads(get_retry(url.replace("query1", "query2")))
        res = j["chart"]["result"][0]
        ts = res.get("timestamp") or []
        cl = res["indicators"]["quote"][0].get("close") or []
        pairs = []
        for t, c in zip(ts, cl):
            if c is None or c <= 0:
                continue
            if pairs and t - pairs[-1][0] < 12 * 3600:
                pairs[-1] = (t, c)  # ASX appends a live bar for the same session: keep one bar per session
            else:
                pairs.append((t, c))
        return res.get("meta", {}), pairs

    def quotes(self, symbols):
        fields = ",".join(["symbol", "shortName", "longName", "currency", "exchange", "fullExchangeName", "marketState", "quoteType",
                           "regularMarketPrice", "regularMarketPreviousClose", "regularMarketChangePercent", "regularMarketTime",
                           "marketCap", "fiftyDayAverage", "twoHundredDayAverage", "fiftyDayAverageChangePercent",
                           "twoHundredDayAverageChangePercent", "exchangeTimezoneName"])
        out = {}
        for i in range(0, len(symbols), 90):
            batch = symbols[i:i + 90]
            url = "https://query1.finance.yahoo.com/v7/finance/quote?symbols=%s&fields=%s&formatted=false" % (urllib.parse.quote(",".join(batch)), fields)
            try:
                j = self.json(url)
                for q in j.get("quoteResponse", {}).get("result", []):
                    out[q["symbol"]] = q
            except Exception as e:
                log("quote batch %d failed: %s" % (i // 90, str(e)[:100]))
            time.sleep(0.4)
        return out

    def summary(self, sym):
        mods = "financialData,defaultKeyStatistics,calendarEvents,summaryProfile,price"
        url = "https://query2.finance.yahoo.com/v10/finance/quoteSummary/%s?modules=%s&formatted=false" % (urllib.parse.quote(sym), mods)
        j = self.json(url)
        return j["quoteSummary"]["result"][0]

    def screener(self, operands, size=250, max_pages=4):
        """Yahoo equity screener. operands: list of {operator, operands}. Returns quote dicts."""
        out = []
        for page in range(max_pages):
            body = {"size": size, "offset": page * size, "sortField": "intradaymarketcap", "sortType": "DESC", "quoteType": "EQUITY",
                    "query": {"operator": "AND", "operands": operands}, "userId": "", "userIdType": "guid"}
            url = "https://query1.finance.yahoo.com/v1/finance/screener?lang=en-US&region=US&formatted=false"
            if self.crumb:
                url += "&crumb=" + urllib.parse.quote(self.crumb)
            raw = get_retry(url, opener=self.op, headers={"Content-Type": "application/json"}, data=json.dumps(body).encode())
            j = json.loads(raw)
            quotes = j["finance"]["result"][0].get("quotes", [])
            out.extend(quotes)
            if len(quotes) < size:
                break
            time.sleep(0.5)
        return out


# ---------------------------------------------------------------- universe
class TableParser(HTMLParser):
    """Collects every <table> as a list of rows, each a list of cell texts."""
    def __init__(self):
        super().__init__()
        self.tables, self.row, self.cell, self.in_cell = [], None, None, False

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self.tables.append([])
        elif tag == "tr" and self.tables:
            self.row = []
        elif tag in ("td", "th") and self.row is not None:
            self.cell, self.in_cell = [], True

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self.in_cell:
            self.row.append(" ".join("".join(self.cell).split()))
            self.in_cell = False
        elif tag == "tr" and self.row is not None and self.tables:
            if self.row:
                self.tables[-1].append(self.row)
            self.row = None

    def handle_data(self, data):
        if self.in_cell:
            self.cell.append(data)


def wiki_tables(page):
    raw = get_retry("https://en.wikipedia.org/wiki/" + page, headers={"User-Agent": "abnormal-movers-screen/1.0 (research desk)"})
    p = TableParser()
    p.feed(raw)
    return p.tables


def u_sp500():
    for t in wiki_tables("List_of_S%26P_500_companies"):
        if t and "Symbol" in t[0][0:2] and len(t) > 400:
            hdr = t[0]
            si, ni = hdr.index("Symbol"), hdr.index("Security")
            sec_i = next((i for i, h in enumerate(hdr) if "Sector" in h), None)
            rows = []
            for r in t[1:]:
                if len(r) <= max(si, ni):
                    continue
                sym = r[si].replace(".", "-").strip()
                if re.fullmatch(r"[A-Z0-9-]{1,6}", sym):
                    rows.append({"symbol": sym, "name": r[ni], "market": "S&P 500", "sector": r[sec_i] if sec_i is not None and len(r) > sec_i else ""})
            return rows
    raise RuntimeError("S&P 500 table not found")


def nasdaq_screener(exchange):
    """nasdaq.com stock screener rows for one exchange (NASDAQ, NYSE, AMEX); marketCap is US$."""
    url = "https://api.nasdaq.com/api/screener/stocks?tableonly=true&limit=6000&exchange=%s&download=true" % exchange
    j = json.loads(get_retry(url, headers={"Accept": "application/json, text/plain, */*", "Origin": "https://www.nasdaq.com", "Referer": "https://www.nasdaq.com/"}))
    return j["data"]["rows"]


def num(v):
    try:
        return float(str(v).replace(",", "").strip() or 0)
    except ValueError:
        return 0.0


def u_nasdaq():
    rows = []
    for r in nasdaq_screener("NASDAQ"):
        sym = r.get("symbol", "").strip()
        name = r.get("name", "")
        cap = num(r.get("marketCap"))
        if cap < NASDAQ_MIN_CAP_USD or not re.fullmatch(r"[A-Z]{1,5}", sym):
            continue
        low = name.lower()
        if any(w in low for w in ("warrant", " right", " unit", "acquisition", "etf", "trust", " fund", "depositary", "preferred", "notes due")):
            continue
        rows.append({"symbol": sym, "name": name, "market": "NASDAQ", "sector": r.get("sector", ""), "industry": r.get("industry", ""), "cap_hint": cap})
    if len(rows) < 300:
        raise RuntimeError("nasdaq screener returned only %d rows" % len(rows))
    return rows


def u_asx(y):
    rows = []
    try:
        qs = y.screener([{"operator": "EQ", "operands": ["region", "au"]}, {"operator": "GT", "operands": ["intradaymarketcap", ASX_MIN_CAP_AUD]}], max_pages=3)
        for q in qs:
            s = q.get("symbol", "")
            if s.endswith(".AX") and q.get("quoteType", "EQUITY") == "EQUITY":
                rows.append({"symbol": s, "name": q.get("shortName") or q.get("longName") or s, "market": "ASX", "sector": q.get("sector", "")})
        rows = rows[:320]
        if len(rows) >= 150:
            log("ASX universe from yahoo screener: %d" % len(rows))
            return rows, "yahoo screener (AU, cap > A$350m)"
    except Exception as e:
        log("ASX screener failed: %s" % str(e)[:100])
    try:
        # ASX's own company directory (no Yahoo crumb needed): market cap in A$, so it also feeds tier 1
        url = ("https://asx.api.markitdigital.com/asx-research/1.0/companies/directory?page=0&itemsPerPage=3000"
               "&order=descending&orderBy=marketCap&includeFilterOptions=false&recentListingsOnly=false")
        items = json.loads(get_retry(url))["data"]["items"]
        for it in items:
            cap = num(it.get("marketCap"))
            code = str(it.get("symbol", "")).strip().upper()
            # 'Not Applic' is listed investment companies and funds
            if cap < ASX_MIN_CAP_AUD or it.get("industry") in ("Not Applic", "Class Pend") or not re.fullmatch(r"[A-Z0-9]{3,4}", code):
                continue
            rows.append({"symbol": code + ".AX", "name": (it.get("displayName") or code).title(), "market": "ASX", "sector": it.get("industry", ""), "cap_hint": cap})
        rows = sorted(rows, key=lambda r: -r["cap_hint"])[:300]
        if len(rows) >= 150:
            log("ASX universe from asx.com.au directory: %d" % len(rows))
            return rows, "asx.com.au directory (top 300 by cap, cap > A$350m)"
        rows = []
    except Exception as e:
        rows = []
        log("ASX directory failed: %s" % str(e)[:100])
    for t in wiki_tables("S%26P/ASX_200"):
        if len(t) > 150:
            hdr = [h.lower() for h in t[0]]
            ci = next((i for i, h in enumerate(hdr) if "code" in h or "ticker" in h or "symbol" in h), 0)
            ni = next((i for i, h in enumerate(hdr) if "company" in h or "name" in h), 1)
            si = next((i for i, h in enumerate(hdr) if "sector" in h), None)
            rows = []
            for r in t[1:]:
                if len(r) <= max(ci, ni):
                    continue
                code = re.sub(r"[^A-Z0-9]", "", r[ci].upper())
                if 2 <= len(code) <= 4:
                    rows.append({"symbol": code + ".AX", "name": r[ni], "market": "ASX", "sector": r[si] if si is not None and len(r) > si else ""})
            if len(rows) > 150:
                log("ASX universe from wikipedia ASX 200: %d" % len(rows))
                return rows, "wikipedia S&P/ASX 200"
    raise RuntimeError("no ASX universe")


def u_nikkei(y):
    try:
        # The Wikipedia page lists components as bullet items: "<li>Toyota Motor Corp. (TYO: 7203)</li>"
        raw = get_retry("https://en.wikipedia.org/wiki/Nikkei_225", headers={"User-Agent": "abnormal-movers-screen/1.0 (research desk)"})
        codes, seen = [], set()
        for m in re.finditer(r"<li[^>]*>(.*?)</li>", raw, re.S):
            text = html.unescape(re.sub(r"<[^>]+>", "", m.group(1)))
            text = " ".join(text.split())
            mm = re.match(r"^(.{2,80}?)\s*\(\s*TYO:\s*(\d{4})\s*\)", text)
            if mm and mm.group(2) not in seen:
                seen.add(mm.group(2))
                codes.append({"symbol": mm.group(2) + ".T", "name": mm.group(1).strip(), "market": "Nikkei 225", "sector": ""})
        if len(codes) >= 200:
            log("Nikkei universe from wikipedia: %d" % len(codes))
            return codes[:235], "wikipedia Nikkei 225"
        log("Nikkei wikipedia parse found only %d codes" % len(codes))
    except Exception as e:
        log("Nikkei wikipedia failed: %s" % str(e)[:100])
    qs = y.screener([{"operator": "EQ", "operands": ["region", "jp"]}, {"operator": "GT", "operands": ["intradaymarketcap", JP_MIN_CAP_JPY]}], max_pages=1)
    rows = [{"symbol": q["symbol"], "name": q.get("shortName") or q["symbol"], "market": "Nikkei 225", "sector": q.get("sector", "")} for q in qs if q.get("symbol", "").endswith(".T")][:225]
    if len(rows) < 100:
        raise RuntimeError("no Nikkei universe")
    log("Nikkei universe from yahoo screener (top by cap): %d" % len(rows))
    return rows, "yahoo screener (JP, top 225 by cap)"


def u_kospi(y):
    try:
        qs = y.screener([{"operator": "EQ", "operands": ["region", "kr"]}, {"operator": "GT", "operands": ["intradaymarketcap", KR_MIN_CAP_KRW]}], max_pages=2)
        rows = [{"symbol": q["symbol"], "name": q.get("shortName") or q["symbol"], "market": "KOSPI", "sector": q.get("sector", "")} for q in qs if q.get("symbol", "").endswith(".KS")][:220]
        if len(rows) >= 100:
            log("KOSPI universe from yahoo screener: %d" % len(rows))
            return rows, "yahoo screener (KR, .KS, top 220 by cap)"
    except Exception as e:
        log("KOSPI screener failed: %s" % str(e)[:100])
    for t in wiki_tables("KOSPI_200"):
        rows = []
        for r in t:
            for c in r[:3]:
                m = re.fullmatch(r"(?:KRX:\s*)?(\d{6})", c.strip())
                if m:
                    name = next((x for x in r if x and not re.fullmatch(r"(?:KRX:\s*)?\d{6}", x.strip())), m.group(1))
                    rows.append({"symbol": m.group(1) + ".KS", "name": name, "market": "KOSPI", "sector": ""})
                    break
        if len(rows) >= 100:
            log("KOSPI universe from wikipedia: %d" % len(rows))
            return rows, "wikipedia KOSPI 200"
    raise RuntimeError("no KOSPI universe")


def build_universe(y):
    uni, sources = {}, {}
    builders = [("S&P 500", lambda: (u_sp500(), "wikipedia S&P 500")), ("NASDAQ", lambda: (u_nasdaq(), "nasdaq.com screener (cap >= US$1bn)")),
                ("ASX", lambda: u_asx(y)), ("Nikkei 225", lambda: u_nikkei(y)), ("KOSPI", lambda: u_kospi(y))]
    for market, fn in builders:
        try:
            rows, src = fn()
            sources[market] = src
            for r in rows:
                if r["symbol"] not in uni:  # S&P 500 wins over NASDAQ for dual members
                    uni[r["symbol"]] = r
            log("%s: %d names" % (market, len(rows)))
        except Exception as e:
            sources[market] = "FAILED: " + str(e)[:120]
            log("universe %s FAILED: %s" % (market, str(e)[:120]))
    for p in PEERS:
        uni.setdefault(p, {"symbol": p, "name": p, "market": "NASDAQ", "sector": ""})
        uni[p]["peer"] = True
    # US$ cap hints for S&P 500 names (and peers) from nasdaq.com, so tier 1 still works when Yahoo quotes are down
    missing = [s for s, r in uni.items() if r["market"] in ("S&P 500", "NASDAQ") and not r.get("cap_hint")]
    if missing:
        caps = {}
        for ex in ("NYSE", "NASDAQ", "AMEX"):
            try:
                for r in nasdaq_screener(ex):
                    caps[r.get("symbol", "").strip().replace("/", "-").replace(".", "-")] = num(r.get("marketCap"))
            except Exception as e:
                log("nasdaq.com %s caps failed: %s" % (ex, str(e)[:80]))
        hit = 0
        for s in missing:
            if caps.get(s):
                uni[s]["cap_hint"] = caps[s]
                hit += 1
        log("US cap hints from nasdaq.com: %d / %d" % (hit, len(missing)))
    return uni, sources


# ---------------------------------------------------------------- maths
def stats(closes):
    """closes: list oldest->newest incl. the current bar. Returns (mean, sd, n) of log returns over the prior 200 sessions."""
    hist = closes[-201:-1]
    rets = [math.log(hist[i] / hist[i - 1]) for i in range(1, len(hist)) if hist[i - 1] > 0]
    n = len(rets)
    if n < 120:
        return None, None, n
    mean = sum(rets) / n
    var = sum((r - mean) ** 2 for r in rets) / (n - 1)
    return mean, math.sqrt(var), n


def usd(v, fx):
    return None if v is None else v * fx


def rnd(v, d=2):
    return None if v is None or (isinstance(v, float) and (math.isnan(v) or math.isinf(v))) else round(v, d)


def clean_name(name):
    n = re.sub(r"[,.]", "", name or "")
    n = re.sub(r"\b(Inc|Ltd|Limited|Corp|Corporation|Co|Company|Holdings|Holding|PLC|plc|Group|NV|SA|AG|SE|Class [A-C]|Common Stock|Ordinary Shares|ADR|ADS)\b.*$", "", n).strip()
    return n or name


def headlines(name, sym, market):
    loc = {"ASX": ("en-AU", "AU", "AU:en"), "Nikkei 225": ("en-US", "US", "US:en"), "KOSPI": ("en-US", "US", "US:en")}.get(market, ("en-US", "US", "US:en"))
    q = '"%s" shares OR stock when:3d' % clean_name(name)
    url = "https://news.google.com/rss/search?q=%s&hl=%s&gl=%s&ceid=%s" % (urllib.parse.quote(q), loc[0], loc[1], loc[2])
    try:
        raw = get_retry(url, tries=2, timeout=25)
        root = ET.fromstring(raw.encode("utf-8", "replace"))
        out = []
        for it in root.iter("item"):
            title = (it.findtext("title") or "").strip()
            src = it.find("source")
            out.append({"title": html.unescape(title), "source": (src.text if src is not None else "") or "", "url": (it.findtext("link") or "").strip(), "published": (it.findtext("pubDate") or "").strip()})
            if len(out) >= 4:
                break
        return out
    except Exception as e:
        return []


# ---------------------------------------------------------------- run
def run(out, notes_in):
    os.makedirs(out, exist_ok=True)
    run_at = now_iso()
    y = Yahoo()
    uni, sources = build_universe(y)
    symbols = sorted(uni)
    log("universe total %d" % len(symbols))

    # FX
    fxq = y.quotes(["AUDUSD=X", "JPY=X", "KRW=X", "GBPUSD=X", "EURUSD=X"])
    fx = {"USD": 1.0}
    try:
        fx["AUD"] = fxq["AUDUSD=X"]["regularMarketPrice"]
        fx["JPY"] = 1.0 / fxq["JPY=X"]["regularMarketPrice"]
        fx["KRW"] = 1.0 / fxq["KRW=X"]["regularMarketPrice"]
        fx["GBP"] = fxq["GBPUSD=X"]["regularMarketPrice"]
        fx["EUR"] = fxq["EURUSD=X"]["regularMarketPrice"]
    except Exception as e:
        log("FX quotes incomplete (%s); trying chart meta" % str(e)[:60])
        for ccy, sym, inv in (("AUD", "AUDUSD=X", False), ("JPY", "JPY=X", True), ("KRW", "KRW=X", True), ("GBP", "GBPUSD=X", False), ("EUR", "EURUSD=X", False)):
            try:
                px = y.chart(sym, "5d")[0]["regularMarketPrice"]
                fx[ccy] = 1.0 / px if inv else px
            except Exception:
                pass
        log("FX %s" % ", ".join("%s %.6g" % kv for kv in sorted(fx.items())))
        fx.setdefault("AUD", 0.66); fx.setdefault("JPY", 1 / 150.0); fx.setdefault("KRW", 1 / 1400.0); fx.setdefault("GBP", 1.3); fx.setdefault("EUR", 1.1)
    fx["GBp"] = fx["GBP"] / 100.0

    # quotes for the whole universe
    quotes = y.quotes(symbols)
    log("quotes received %d / %d" % (len(quotes), len(symbols)))
    quote_mode = "v7 quote"
    if len(quotes) < len(symbols) * 0.5:
        quote_mode = "chart meta fallback"
        log("quote endpoint thin; falling back to chart meta for every symbol (slow)")

        def meta_quote(s):
            try:
                meta, pairs = y.chart(s, "5d")
                price = meta.get("regularMarketPrice")
                # chartPreviousClose is the close before the 5d window, not yesterday: take the prior daily bar
                rmt = meta.get("regularMarketTime")
                prev = None
                if pairs:
                    if rmt and abs(rmt - pairs[-1][0]) < 20 * 3600:
                        prev = pairs[-2][1] if len(pairs) >= 2 else None
                    else:
                        prev = pairs[-1][1]
                prev = prev or meta.get("previousClose")
                if not price or not prev:
                    return s, None
                return s, {"symbol": s, "regularMarketPrice": price, "regularMarketPreviousClose": prev, "regularMarketChangePercent": (price / prev - 1) * 100,
                           "currency": meta.get("currency"), "exchange": meta.get("exchangeName"), "fullExchangeName": meta.get("fullExchangeName"),
                           "marketState": "", "regularMarketTime": meta.get("regularMarketTime"), "marketCap": uni[s].get("cap_hint"),
                           "shortName": meta.get("shortName"), "longName": meta.get("longName"), "quoteType": meta.get("instrumentType", "EQUITY")}
            except Exception:
                return s, None
        with cf.ThreadPoolExecutor(max_workers=6) as ex:
            for s, q in ex.map(meta_quote, symbols):
                if q:
                    quotes[s] = q

    # first pass: candidates
    cands = []
    per_market = {m: {"market": m, "universe": 0, "quoted": 0, "candidates": 0, "flagged": 0, "tier1": 0, "tier2": 0, "tier3": 0, "excluded": 0,
                      "state": "", "last_trade": None, "source": sources.get(m, ""), "run_at": run_at, "quote_mode": quote_mode} for m in MARKETS}
    for s in symbols:
        u = uni[s]
        pm = per_market[u["market"]]
        pm["universe"] += 1
        q = quotes.get(s)
        if not q or q.get("quoteType") not in (None, "", "EQUITY"):
            continue
        day = q.get("regularMarketChangePercent")
        price = q.get("regularMarketPrice")
        if day is None or not price:
            continue
        pm["quoted"] += 1
        ccy = q.get("currency") or "USD"
        f = fx.get(ccy, 1.0)
        cap_usd = usd(q.get("marketCap"), f)
        if q.get("marketState") and not pm["state"]:
            pm["state"] = q["marketState"]
        if q.get("regularMarketTime") and (pm["last_trade"] is None or q["regularMarketTime"] > pm["last_trade"]):
            pm["last_trade"] = q["regularMarketTime"]
        ma50 = q.get("fiftyDayAverageChangePercent")
        ma50 = ma50 * 100 if ma50 is not None and abs(ma50) < 5 else ma50  # v7 gives a fraction
        is_cand = abs(day) >= CAND_DAY or (ma50 is not None and abs(ma50) >= CAND_MA50) or (cap_usd is not None and cap_usd >= LARGE_CAP_USD and abs(day) >= CAND_LARGE_DAY) or u.get("peer")
        if is_cand:
            cands.append(s)
            pm["candidates"] += 1
    for pm in per_market.values():
        if pm["last_trade"]:
            pm["last_trade"] = dt.datetime.fromtimestamp(pm["last_trade"], dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    log("candidates %d" % len(cands))

    # second pass: charts
    def analyse(s):
        try:
            meta, pairs = y.chart(s, "1y")
        except Exception as e:
            return s, None
        if len(pairs) < 10:
            return s, None
        q = quotes[s]
        closes = [c for _, c in pairs]
        price = q["regularMarketPrice"]
        last_ts = pairs[-1][0]
        qt = q.get("regularMarketTime") or last_ts
        same_session = abs(qt - last_ts) < 20 * 3600
        if not same_session:
            closes.append(price)  # chart lags the quote: treat the quote as the current bar
        wk = (closes[-1] / closes[-6] - 1) * 100 if len(closes) >= 6 else None
        mean, sd, n = stats(closes)
        day = q["regularMarketChangePercent"]
        z = ((math.log(1 + day / 100) - mean) / sd) if sd else None
        ma200 = q.get("twoHundredDayAverage") or (sum(closes[-200:]) / len(closes[-200:]) if len(closes) >= 100 else None)
        ma200_dev = (price / ma200 - 1) * 100 if ma200 else None
        spark = [(dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%Y-%m-%d"), c) for t, c in pairs[-30:]]
        return s, {"closes": closes, "wk": wk, "z": z, "sigma": sd * 100 if sd else None, "n": n, "ma200_dev": ma200_dev, "spark": spark}

    charts = {}
    with cf.ThreadPoolExecutor(max_workers=6) as ex:
        for s, r in ex.map(analyse, cands):
            if r:
                charts[s] = r
    log("charts ok %d / %d" % (len(charts), len(cands)))

    # flags
    rows = []
    for s in cands:
        c = charts.get(s)
        if not c:
            continue
        q, u = quotes[s], uni[s]
        ccy = q.get("currency") or "USD"
        f = fx.get(ccy, 1.0)
        cap_usd = usd(q.get("marketCap"), f)
        day = q["regularMarketChangePercent"]
        tiers = []
        if cap_usd is not None and cap_usd >= LARGE_CAP_USD and c["z"] is not None and abs(c["z"]) >= Z_MIN:
            tiers.append(1)
        if c["wk"] is not None and abs(c["wk"]) >= WEEK_PCT:
            tiers.append(2)
        if abs(day) >= DAY_PCT:
            tiers.append(3)
        peer = bool(u.get("peer"))
        if not tiers and not peer:
            continue
        rows.append({"symbol": s, "name": q.get("longName") or q.get("shortName") or u["name"], "market": u["market"], "exchange": q.get("fullExchangeName") or q.get("exchange") or "",
                     "sector": u.get("sector") or "", "industry": u.get("industry") or "", "currency": ccy, "price": rnd(q["regularMarketPrice"], 4),
                     "day_pct": rnd(day), "wk_pct": rnd(c["wk"]), "z": rnd(c["z"], 1), "sigma200": rnd(c["sigma"]), "n_sessions": c["n"], "ma200_dev": rnd(c["ma200_dev"], 1),
                     "mcap_usd": rnd(cap_usd, 0), "tier": min(tiers) if tiers else None, "tiers": tiers, "peer": peer, "market_state": q.get("marketState") or "",
                     "quote_time": dt.datetime.fromtimestamp(q["regularMarketTime"], dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if q.get("regularMarketTime") else None,
                     "run_at": run_at, "_spark": c["spark"]})
    log("flagged incl peers %d" % len(rows))

    # fundamentals, stage filter, calendar
    def fundamentals(r):
        try:
            sm = y.summary(r["symbol"])
        except Exception as e:
            return r["symbol"], {"fund_ok": False}
        fd, ks, ce, sp, pr = (sm.get(k) or {} for k in ("financialData", "defaultKeyStatistics", "calendarEvents", "summaryProfile", "price"))
        f = fx.get(fd.get("financialCurrency") or r["currency"], fx.get(r["currency"], 1.0))
        rev = usd(fd.get("totalRevenue"), f)
        debt, cash, ebitda = usd(fd.get("totalDebt"), f), usd(fd.get("totalCash"), f), usd(fd.get("ebitda"), f)
        ev = usd(ks.get("enterpriseValue"), fx.get(r["currency"], 1.0))
        nd = (debt - cash) if debt is not None and cash is not None else None
        earn = (ce.get("earnings") or {}).get("earningsDate") or []
        earn = [e for e in earn if isinstance(e, (int, float))]
        nxt = dt.datetime.fromtimestamp(min(earn), dt.timezone.utc).strftime("%Y-%m-%d") if earn else None
        exd = ce.get("exDividendDate")
        exd = dt.datetime.fromtimestamp(exd, dt.timezone.utc).strftime("%Y-%m-%d") if isinstance(exd, (int, float)) else None
        return r["symbol"], {"fund_ok": True, "revenue_usd": rnd(rev, 0), "debt_usd": rnd(debt, 0), "cash_usd": rnd(cash, 0), "net_debt_usd": rnd(nd, 0), "ebitda_usd": rnd(ebitda, 0),
                             "ev_usd": rnd(ev, 0), "nd_ebitda": rnd(nd / ebitda, 1) if nd is not None and ebitda and ebitda > 0 else None,
                             "debt_ev": rnd(debt / ev * 100, 1) if debt is not None and ev and ev > 0 else None, "next_earnings": nxt, "ex_div": exd,
                             "sector": sp.get("sector") or r["sector"], "industry": sp.get("industry") or r["industry"], "summary": (sp.get("longBusinessSummary") or "")[:220]}

    funds = {}
    with cf.ThreadPoolExecutor(max_workers=4) as ex:
        for s, fr in ex.map(fundamentals, rows):
            funds[s] = fr
    today = dt.date.today().isoformat()
    for r in rows:
        r.update(funds.get(r["symbol"], {"fund_ok": False}))
        rev = r.get("revenue_usd")
        r["excluded"] = None
        if r.get("fund_ok") and rev is not None and rev < REV_MIN_USD and not r["peer"]:
            r["excluded"] = "revenue below US$50m (pre-revenue)"
        elif r.get("fund_ok") and rev is None and not r["peer"]:
            r["excluded"] = "no revenue reported (pre-revenue)"
        nxt = r.get("next_earnings")
        exd = r.get("ex_div")
        cat = []
        if nxt and nxt >= today:
            cat.append("Earnings " + nxt)
        if exd and exd >= today:
            cat.append("Ex-div " + exd)
        r["catalyst"] = "; ".join(cat) if cat else ("Last reported " + nxt if nxt else "")
    log("fundamentals ok %d / %d" % (sum(1 for r in rows if r.get("fund_ok")), len(rows)))

    # headlines + notes
    notes = {}
    if notes_in and os.path.exists(notes_in):
        try:
            notes = json.load(open(notes_in, encoding="utf-8")).get("notes", {})
        except Exception:
            notes = {}
    cutoff = (dt.datetime.utcnow() - dt.timedelta(days=NOTE_TTL_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    notes = {k: v for k, v in notes.items() if v.get("at", "") >= cutoff}
    need = []
    want_news = [r for r in rows if not r["excluded"] and (r["tiers"] or r["peer"])]
    with cf.ThreadPoolExecutor(max_workers=4) as ex:
        for r, hl in zip(want_news, ex.map(lambda r: headlines(r["name"], r["symbol"], r["market"]), want_news)):
            r["headlines"] = hl
    for r in rows:
        r.setdefault("headlines", [])
        key = r["symbol"] + "|" + (r.get("quote_time") or run_at)[:10]
        r["note_key"] = key
        cached = notes.get(r["symbol"])
        if cached and cached.get("key") == key:
            r["note"] = cached["note"]
        else:
            r["note"] = cached["note"] if cached else ""
            if r["tiers"] and not r["excluded"]:
                need.append({"symbol": r["symbol"], "name": r["name"], "market": r["market"], "day_pct": r["day_pct"], "wk_pct": r["wk_pct"], "z": r["z"],
                             "tier": r["tier"], "catalyst": r["catalyst"], "summary": r.get("summary", ""), "headlines": r["headlines"]})
    log("notes needed %d (cached %d)" % (len(need), len(notes)))

    for pm in per_market.values():
        for r in rows:
            if r["market"] != pm["market"]:
                continue
            if r["excluded"]:
                pm["excluded"] += 1
            elif r["tiers"] and not r["peer"]:
                pm["flagged"] += 1
                for t in r["tiers"]:
                    pm["tier%d" % t] += 1

    sparks = [{"symbol": r["symbol"], "date": d, "close": rnd(c, 4)} for r in rows for d, c in r.pop("_spark")]
    rows.sort(key=lambda r: (r["peer"], r["tier"] or 9, -abs(r["z"] or 0), -abs(r["day_pct"] or 0)))
    json.dump(rows, open(os.path.join(out, "movers.json"), "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    json.dump(list(per_market.values()), open(os.path.join(out, "markets.json"), "w", encoding="utf-8"), ensure_ascii=False)
    json.dump(sparks, open(os.path.join(out, "sparks.json"), "w", encoding="utf-8"), separators=(",", ":"))
    json.dump(need, open(os.path.join(out, "needs_notes.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    json.dump({"notes": notes}, open(os.path.join(out, "notes.json"), "w", encoding="utf-8"), ensure_ascii=False)
    open(os.path.join(out, "log.txt"), "w").write("\n".join(LOG))
    log("done: %d rows, %d need notes" % (len(rows), len(need)))


def merge_notes(out, new_path):
    new = json.load(open(new_path, encoding="utf-8"))
    rows = json.load(open(os.path.join(out, "movers.json"), encoding="utf-8"))
    cache = json.load(open(os.path.join(out, "notes.json"), encoding="utf-8"))
    at = now_iso()
    n = 0
    for r in rows:
        note = new.get(r["symbol"])
        if note:
            r["note"] = note.strip()
            cache["notes"][r["symbol"]] = {"note": r["note"], "key": r["note_key"], "at": at}
            n += 1
    json.dump(rows, open(os.path.join(out, "movers.json"), "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    json.dump(cache, open(os.path.join(out, "notes.json"), "w", encoding="utf-8"), ensure_ascii=False)
    print("merged %d notes" % n)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["run", "merge-notes"])
    ap.add_argument("--out", default="out")
    ap.add_argument("--notes-in")
    ap.add_argument("--new")
    a = ap.parse_args()
    if a.cmd == "run":
        run(a.out, a.notes_in)
    else:
        merge_notes(a.out, a.new)
