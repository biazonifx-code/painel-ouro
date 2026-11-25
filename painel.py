#!/usr/bin/env python3
"""
Painel Ouro (versao web) - port do Painel_Ouro_v2.mq5

Calcula o mesmo score macro (-10..+10), confirmacoes, votacao tecnica e gatilho
do EA, usando dados publicos (Yahoo Finance + FRED), e grava docs/data.json.
A pagina docs/index.html le esse arquivo. Opcionalmente avisa no Telegram.

Uso local:   python painel.py
No GitHub:   rodado pelo workflow .github/workflows/painel.yml
"""
import json
import math
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import requests

BASE = Path(__file__).parent
CFG_PATH = BASE / "config.json"
OUT_PATH = BASE / "docs" / "data.json"

GEO_VALUES = {"baixo": -0.5, "neutro": 0.0, "moderado": 0.5, "elevado": 1.0}
CAPS = [1.5, 2.5, 2.5, 1.0, 0.5, 1.0]
FACTOR_NAMES = [
    "Momentum do ouro",
    "Dólar (DXY)",
    "Juros / yields EUA",
    "Risco fiscal / geopolítico",
    "Recompras do Tesouro",
    "RSI(14) / extremos",
]
TRIG_NAMES = [
    "Estocástico (14,3)",
    "MACD histograma",
    "EMA 9 x EMA 21",
    "Bollinger %B (20,2)",
    "Divergência RSI",
    "Padrão de vela",
]
VOTE_NAMES = ["EMA", "P/EMA50", "RSI", "MACD", "MOM"]


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def sgn(v, dig=2):
    return f"{v:+.{dig}f}"


# ----------------------------------------------------------------------------
# Indicadores (mesma convencao do MT5: ATR = SMA do TR, sinal do MACD = SMA,
# desvio padrao populacional no Bollinger, RSI com suavizacao de Wilder)
# ----------------------------------------------------------------------------
def rsi(close, n=14):
    d = close.diff()
    up = d.clip(lower=0)
    dn = -d.clip(upper=0)
    au = up.ewm(alpha=1 / n, adjust=False).mean()
    ad = dn.ewm(alpha=1 / n, adjust=False).mean()
    rs = au / ad.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(100.0)


def atr(df, n=14):
    pc = df["Close"].shift(1)
    tr = pd.concat(
        [df["High"] - df["Low"], (df["High"] - pc).abs(), (df["Low"] - pc).abs()],
        axis=1,
    ).max(axis=1)
    return tr.rolling(n).mean()


def stoch(df, k=14, slow=3, dsm=3):
    ll = df["Low"].rolling(k).min()
    hh = df["High"].rolling(k).max()
    raw = (df["Close"] - ll) / (hh - ll).replace(0, np.nan) * 100
    kk = raw.rolling(slow).mean()
    return kk, kk.rolling(dsm).mean()


def ema(s, n):
    return s.ewm(span=n, adjust=False).mean()


def macd_hist(close):
    m = ema(close, 12) - ema(close, 26)
    sig = m.rolling(9).mean()
    return m - sig


def bands(close, n=20, k=2.0):
    ma = close.rolling(n).mean()
    sd = close.rolling(n).std(ddof=0)
    return ma + k * sd, ma - k * sd


def momentum(close, n=10):
    return close / close.shift(n) * 100


def at(series, sh):
    """Valor no shift 'sh' (0 = barra atual, 1 = ultima fechada)."""
    v = series.iloc[-1 - sh]
    return None if pd.isna(v) else float(v)


# ----------------------------------------------------------------------------
# Sinais lentos do gatilho (barras fechadas)
# ----------------------------------------------------------------------------
def rsi_div(trig, rsi_trig):
    if len(trig) < 41:
        return 0
    lo = trig["Low"].values[::-1][:40]
    hi = trig["High"].values[::-1][:40]
    rs = rsi_trig.values[::-1][:40]
    a, b = 1, 9
    for i in range(1, 9):
        if lo[i] < lo[a]:
            a = i
    for i in range(9, 31):
        if lo[i] < lo[b]:
            b = i
    if lo[a] < lo[b] and rs[a] > rs[b] and rs[b] < 50:
        return 1
    a, b = 1, 9
    for i in range(1, 9):
        if hi[i] > hi[a]:
            a = i
    for i in range(9, 31):
        if hi[i] > hi[b]:
            b = i
    if hi[a] > hi[b] and rs[a] < rs[b] and rs[b] > 50:
        return -1
    return 0


def candle(trig):
    if len(trig) < 4:
        return 0, "nenhum"
    r1 = trig.iloc[-2]
    r2 = trig.iloc[-3]
    o1, c1, h1, l1 = r1["Open"], r1["Close"], r1["High"], r1["Low"]
    o2, c2 = r2["Open"], r2["Close"]
    body = abs(c1 - o1)
    lw = min(o1, c1) - l1
    uw = h1 - max(o1, c1)
    if c2 < o2 and c1 > o1 and c1 >= o2 and o1 <= c2:
        return 1, "engolfo de alta"
    if c2 > o2 and c1 < o1 and c1 <= o2 and o1 >= c2:
        return -1, "engolfo de baixa"
    if body > 0 and lw >= 2 * body and uw <= 0.5 * body:
        return 1, "martelo"
    if body > 0 and uw >= 2 * body and lw <= 0.5 * body:
        return -1, "estrela cadente"
    return 0, "nenhum"


# ----------------------------------------------------------------------------
# Calculo principal (funcao pura: recebe dados, devolve o dicionario do painel)
# ----------------------------------------------------------------------------
def compute(cfg, trig, regime, daily, dxy_daily, fred, watch):
    sh = 1 if cfg["closed_bar"] else 0

    price = float(trig["Close"].iloc[-1])
    pc = float(daily["Close"].iloc[-2])
    gchg = (price / pc - 1.0) * 100.0

    # DXY: ultimo valor contra o fechamento anterior
    dxy = dchg = None
    if dxy_daily is not None and len(dxy_daily) >= 2:
        dxy = float(dxy_daily["Close"].iloc[-1])
        dchg = (dxy / float(dxy_daily["Close"].iloc[-2]) - 1.0) * 100.0

    # --- indicadores do gatilho (timeframe curto) -------------------------
    c = trig["Close"]
    rsi_trig = rsi(c)
    atr_s = atr(trig)
    k_s, d_s = stoch(trig)
    mh = macd_hist(c)
    e9, e21, e50 = ema(c, 9), ema(c, 21), ema(c, 50)
    bu, bl = bands(c)
    mom = momentum(c)

    g2_rsi = at(rsi_trig, sh)
    g_atr = at(atr_s, sh)
    k, d = at(k_s, sh), at(d_s, sh)
    v9, v21, v50 = at(e9, sh), at(e21, sh), at(e50, sh)
    bu0, bl0 = at(bu, sh), at(bl, sh)
    m0, m1 = at(mh, sh), at(mh, sh + 1)
    mo = at(mom, sh)
    g_rsi = at(rsi(regime["Close"]), sh)  # RSI do regime
    needed = [g2_rsi, g_atr, k, d, v9, v21, bu0, bl0, m0, m1, g_rsi]
    if any(x is None for x in needed) or g_atr <= 0:
        raise RuntimeError("histórico insuficiente para os indicadores")

    refp = price if sh == 0 else float(c.iloc[-1 - sh])
    pb = (refp - bl0) / (bu0 - bl0) * 100.0 if bu0 > bl0 else 50.0

    div = rsi_div(trig, rsi_trig)
    cdir, cname = candle(trig)

    bull = [0] * 6
    bear = [0] * 6
    vals = [""] * 6
    if k < 20 or (k > d and k < 40):
        bull[0] = 1
    if k > 80 or (k < d and k > 60):
        bear[0] = 1
    vals[0] = f"%K {k:.0f} / %D {d:.0f}"
    if m0 > 0 and m0 > m1:
        bull[1] = 1
    if m0 < 0 and m0 < m1:
        bear[1] = 1
    vals[1] = f"{sgn(m0)} (ant. {m1:.2f})"
    if v9 > v21:
        bull[2] = 1
    else:
        bear[2] = 1
    vals[2] = f"{v9:.2f} vs {v21:.2f}"
    if pb < 20:
        bull[3] = 1
    if pb > 80:
        bear[3] = 1
    vals[3] = f"{pb:.0f}% da faixa"
    if div > 0:
        bull[4] = 1
        vals[4] = "divergência de alta"
    elif div < 0:
        bear[4] = 1
        vals[4] = "divergência de baixa"
    else:
        vals[4] = "nenhuma"
    if cdir > 0:
        bull[5] = 1
    if cdir < 0:
        bear[5] = 1
    vals[5] = cname
    n_bull, n_bear = sum(bull), sum(bear)

    # --- votacao tecnica (compra / neutro / venda) ------------------------
    band = 0.1 * g_atr
    tv = [0] * 5
    tv[0] = 1 if v9 - v21 > band else (-1 if v21 - v9 > band else 0)
    tv[1] = 0 if v50 is None else (1 if price - v50 > band else (-1 if v50 - price > band else 0))
    tv[2] = 1 if g2_rsi > 55 else (-1 if g2_rsi < 45 else 0)
    tv[3] = 1 if m0 > 0 else (-1 if m0 < 0 else 0)
    tv[4] = 0 if mo is None else (1 if mo > 100.05 else (-1 if mo < 99.95 else 0))
    tv_buy = sum(1 for x in tv if x > 0)
    tv_sell = sum(1 for x in tv if x < 0)
    tech_score = (tv_buy - tv_sell) * 100.0 / 5.0
    if tech_score >= 60:
        tech_cls = "COMPRA FORTE"
    elif tech_score >= 20:
        tech_cls = "COMPRA"
    elif tech_score > -20:
        tech_cls = "NEUTRO"
    elif tech_score > -60:
        tech_cls = "VENDA"
    else:
        tech_cls = "VENDA FORTE"

    # --- fatores do score -------------------------------------------------
    f = [0.0] * 6
    sub = [""] * 6
    f[0] = clamp(gchg * 1.25, -CAPS[0], CAPS[0])
    sub[0] = f"{sgn(gchg)}% no dia"

    if dchg is not None:
        f[1] = clamp(-dchg * 6.5, -CAPS[1], CAPS[1])
        sub[1] = f"DXY {dxy:.2f} ({sgn(dchg)}%)"
    else:
        sub[1] = "sem dados do DXY"

    yok = fred is not None
    d10 = 0.0
    if yok:
        d10 = fred["y10"] - fred["y10p"]
        f[2] = clamp(-d10 * 18.0, -CAPS[2], CAPS[2])
        sub[2] = f"10A {fred['y10']:.2f}% ({sgn(d10)}pp) / 30A {fred['y30']:.2f}%"
    else:
        sub[2] = "sem dados do FRED"

    geo_key = str(cfg.get("geo", "neutro")).lower()
    geo = GEO_VALUES.get(geo_key, 0.0)
    f[3] = clamp(geo, -CAPS[3], CAPS[3])
    sub[3] = f"{geo_key.capitalize()} (manual)"

    tes = bool(cfg.get("tes", False))
    f[4] = clamp(0.5, -CAPS[4], CAPS[4]) if tes else 0.0
    sub[4] = ("Ativo" if tes else "Inativo") + " (manual)"

    f[5] = clamp(-(g_rsi - 50.0) / 50.0, -CAPS[5], CAPS[5])
    sub[5] = f"macro {g_rsi:.1f} / " + (
        "sobrecomprado" if g_rsi > 70 else "sobrevendido" if g_rsi < 30 else "neutro"
    )

    score = clamp(sum(f), -10.0, 10.0)

    # --- confirmacoes -----------------------------------------------------
    direction = 1 if score >= 0 else -1
    dir_trig = n_bull if direction > 0 else n_bear
    opp_trig = n_bear if direction > 0 else n_bull

    ed = None
    if len(daily) >= 50:
        ed = float(ema(daily["Close"], 50).iloc[-1])

    conf = [False] * 5
    cval = [""] * 5
    conf[0] = dchg is not None and direction * (-dchg) > 0
    cval[0] = f"{sgn(dchg)}%" if dchg is not None else "n/d"
    conf[1] = yok and direction * (-d10) > 0
    cval[1] = f"{sgn(d10, 3)}pp" if yok else "n/d"
    conf[2] = direction * gchg > 0
    cval[2] = f"{sgn(gchg)}%"
    conf[3] = ed is not None and direction * (price - ed) > 0
    cval[3] = f"{sgn(price - ed)} vs EMA" if ed is not None else "n/d"
    conf[4] = dir_trig >= 3
    cval[4] = f"{dir_trig}/6 gatilhos"
    n_conf = sum(conf)
    contra = opp_trig >= 4 and opp_trig > dir_trig

    if abs(score) >= cfg["min_score"] and n_conf >= cfg["min_conf"] and not contra:
        sig = "COMPRA" if direction > 0 else "VENDA"
    else:
        sig = "AGUARDAR"
    conf_pct = min(95.0, abs(score) / 10.0 * 100.0 * 0.6 + n_conf / 5.0 * 100.0 * 0.4)

    # --- gatilho de scalp -------------------------------------------------
    trig_dir = 1 if tv_buy > tv_sell else (-1 if tv_sell > tv_buy else 0)
    spread = float(cfg.get("assumed_spread", 0.0))
    spread_ok = spread <= 0 or g_atr >= cfg["min_atr_spread"] * spread
    align_ok = trig_dir == 0 or trig_dir == direction
    if sig != "AGUARDAR" and trig_dir != 0 and align_ok and spread_ok:
        trig_sig = "COMPRA" if trig_dir > 0 else "VENDA"
    else:
        trig_sig = "AGUARDAR"

    return {
        "price": round(price, 2),
        "gold_chg": round(gchg, 3),
        "dxy": None if dxy is None else {"value": round(dxy, 2), "chg": round(dchg, 3)},
        "yields": fred,
        "score": round(score, 2),
        "direction": direction,
        "signal": sig,
        "conf_pct": round(conf_pct),
        "n_conf": n_conf,
        "min_score": cfg["min_score"],
        "min_conf": cfg["min_conf"],
        "contra": contra,
        "factors": [
            {"name": FACTOR_NAMES[i], "value": round(f[i], 2), "cap": CAPS[i], "sub": sub[i]}
            for i in range(6)
        ],
        "confirmations": [
            {"name": n, "ok": bool(conf[i]), "value": cval[i]}
            for i, n in enumerate(
                ["DXY a favor", "Yields a favor", "Ouro no dia", "Acima/abaixo da EMA50 D1", "Gatilhos técnicos"]
            )
        ],
        "trigger": {
            "tf": cfg["tf_trigger"],
            "signal": trig_sig,
            "raw_dir": trig_dir,
            "aligned": bool(align_ok),
            "spread_ok": bool(spread_ok),
            "atr": round(g_atr, 2),
            "assumed_spread": spread,
            "items": [
                {"name": TRIG_NAMES[i], "bull": bool(bull[i]), "bear": bool(bear[i]), "value": vals[i]}
                for i in range(6)
            ],
        },
        "tech": {
            "score": tech_score,
            "label": tech_cls,
            "buy": tv_buy,
            "sell": tv_sell,
            "neutral": 5 - tv_buy - tv_sell,
            "votes": [{"name": VOTE_NAMES[i], "vote": tv[i]} for i in range(5)],
        },
        "watch": watch,
    }


# ----------------------------------------------------------------------------
# Coleta de dados
# ----------------------------------------------------------------------------
def history(ticker, period, interval, tries=3):
    import yfinance as yf

    last = None
    for i in range(tries):
        try:
            df = yf.Ticker(ticker).history(period=period, interval=interval, auto_adjust=False)
            df = df.dropna(subset=["Open", "High", "Low", "Close"])
            if len(df):
                return df
        except Exception as e:  # noqa: BLE001
            last = e
        time.sleep(2 * (i + 1))
    raise RuntimeError(f"Yahoo sem dados para {ticker} {interval}" + (f": {last}" if last else ""))


def fred_last_two(series_id):
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"
    r = requests.get(url, timeout=20)
    r.raise_for_status()
    vals = []
    for ln in reversed(r.text.strip().splitlines()[1:]):
        parts = ln.split(",")
        if len(parts) < 2 or parts[1].strip() in (".", ""):
            continue
        vals.append(float(parts[1]))
        if len(vals) == 2:
            break
    if len(vals) < 2:
        raise RuntimeError(f"FRED sem dados para {series_id}")
    return vals[0], vals[1]


def get_fred():
    try:
        y10, y10p = fred_last_two("DGS10")
        y30, y30p = fred_last_two("DGS30")
        return {"y10": y10, "y10p": y10p, "y30": y30, "y30p": y30p}
    except Exception as e:  # noqa: BLE001
        print("FRED falhou:", e, file=sys.stderr)
        return None


def get_watch(cfg):
    out = []
    for item in cfg["watchlist"]:
        try:
            d = history(item["ticker"], "5d", "1d", tries=2)
            if len(d) < 2:
                raise RuntimeError("poucos dados")
            px = float(d["Close"].iloc[-1])
            chg = (px / float(d["Close"].iloc[-2]) - 1.0) * 100.0
            out.append({"name": item["label"], "price": px, "chg": round(chg, 3), "ok": True})
        except Exception as e:  # noqa: BLE001
            print("watch falhou", item["ticker"], e, file=sys.stderr)
            out.append({"name": item["label"], "ok": False})
    return out


# ----------------------------------------------------------------------------
# Telegram
# ----------------------------------------------------------------------------
def telegram(text):
    token = os.environ.get("TELEGRAM_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat, "text": text},
            timeout=20,
        )
    except Exception as e:  # noqa: BLE001
        print("Telegram falhou:", e, file=sys.stderr)


def maybe_alert(new, old, cfg):
    if not new or "signal" not in new:
        return
    old_sig = (old or {}).get("signal")
    old_trig = ((old or {}).get("trigger") or {}).get("signal")
    new_sig = new["signal"]
    new_trig = new["trigger"]["signal"]
    if old is None or (new_sig == old_sig and new_trig == old_trig):
        return
    # mesmo criterio do EA: so avisa gatilho acionavel; opcionalmente tambem o sinal macro
    actionable = new_trig in ("COMPRA", "VENDA")
    macro_changed = new_sig != old_sig and cfg.get("alert_macro_change", True) and new_sig != "AGUARDAR"
    if not (actionable or macro_changed):
        return
    msg = (
        f"Painel Ouro XAUUSD\n"
        f"Gatilho {new_trig} ({new['trigger']['tf']}) | macro {new_sig}\n"
        f"Score {sgn(new['score'], 1)} | {new['n_conf']}/5 confirmações | ATR {new['trigger']['atr']}\n"
        f"Preço {new['price']}"
    )
    telegram(msg)


# ----------------------------------------------------------------------------
def main():
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    old = None
    if OUT_PATH.exists():
        try:
            old = json.loads(OUT_PATH.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            old = None

    now = datetime.now(timezone.utc)
    try:
        t = cfg["tickers"]
        trig = history(t["gold"], "5d", cfg["tf_trigger"])
        regime = history(t["gold"], "1mo", cfg["tf_regime"])
        daily = history(t["gold"], "1y", "1d")
        try:
            dxy_daily = history(t["dxy"], "10d", "1d", tries=2)
        except Exception as e:  # noqa: BLE001
            print("DXY falhou:", e, file=sys.stderr)
            dxy_daily = None
        fred = get_fred()
        watch = get_watch(cfg) if cfg.get("watchlist") else []

        data = compute(cfg, trig, regime, daily, dxy_daily, fred, watch)
        last_bar = trig.index[-1].tz_convert("UTC") if trig.index.tz else trig.index[-1]
        data["data_time"] = last_bar.isoformat()
        data["updated"] = now.isoformat()
        data["error"] = None
    except Exception as e:  # noqa: BLE001
        print("ERRO:", e, file=sys.stderr)
        if old is None:
            old = {}
        data = dict(old)
        data["error"] = str(e)
        data["error_at"] = now.isoformat()
        data.setdefault("updated", None)

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")

    if not data.get("error"):
        maybe_alert(data, old, cfg)
    print("ok:", data.get("signal"), data.get("score"), "erro:", data.get("error"))


if __name__ == "__main__":
    main()
