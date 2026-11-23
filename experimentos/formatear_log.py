#!/usr/bin/env python3
"""Convierte el log de pytest (TRACE_JSON / APP_LOG_JSON / SAGA_EVENT_JSON /
EVIDENCE_JSON) en un reporte legible, agrupado por test.

Uso:
    python formatear_log.py experimentos-XXXX.log            # -> experimentos-XXXX.legible.txt
    python formatear_log.py experimentos-XXXX.log --md       # -> .md
    python formatear_log.py experimentos-XXXX.log --full     # sin resumir los trace
"""
import json, re, sys
from pathlib import Path

TAGS = ("TRACE_JSON", "APP_LOG_JSON", "SAGA_EVENT_JSON", "EVIDENCE_JSON")
TAG_RE = re.compile(r"(%s) (\{.*\})\s*$" % "|".join(TAGS))
TEST_RE = re.compile(r"^(\S+\.py::\S+)\s*")
RESULT_RE = re.compile(r"^(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\b")


def leer(path):
    raw = Path(path).read_bytes()
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        txt = raw.decode("utf-16")
    else:
        try:
            txt = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            txt = raw.decode("latin-1")
    return txt.replace("\r\n", "\n").split("\n")


def parsear(lineas):
    tests, actual, cabecera, resumen = [], None, [], []
    for ln in lineas:
        m = TEST_RE.match(ln)
        if m:
            actual = {"nombre": m.group(1), "eventos": [], "resultado": "?", "otras": []}
            tests.append(actual)
            ln = ln[m.end():]
        if actual is None:
            cabecera.append(ln)
            continue
        m = TAG_RE.search(ln)
        if m:
            try:
                actual["eventos"].append((m.group(1), json.loads(m.group(2))))
                continue
            except json.JSONDecodeError:
                pass
        r = RESULT_RE.match(ln)
        if r:
            actual["resultado"] = r.group(1)
        elif ln.strip():
            (resumen if ln.startswith(("=", "-- Docs", "  ", "..")) else actual["otras"]).append(ln)
    return cabecera, tests, resumen


def corto(v, n=90):
    s = json.dumps(v, ensure_ascii=False) if not isinstance(v, str) else v
    return s if len(s) <= n else s[: n - 1] + "…"


def fmt_trace(d, full):
    seq = d.get("sequence", "-")
    accion = d.get("action", "?")
    partes = [f"[{seq:>2}] {accion}"]
    req, resp = d.get("request"), d.get("response")
    if isinstance(req, dict) and "method" in req:
        partes.append(f"{req['method']} {req['path']}")
    if isinstance(resp, dict) and "status_code" in resp:
        ms = resp.get("elapsed_ms")
        partes.append(f"-> {resp['status_code']}" + (f" ({ms:.0f} ms)" if isinstance(ms, (int, float)) else ""))
    if d.get("outcome"):
        partes.append(f"[{d['outcome']}]")
    if d.get("circuit"):
        partes.append(f"circuito={d['circuit']}")
    if "bulkhead_capacity" in d:
        partes.append(f"bulkhead_cap={d['bulkhead_capacity']}")
    linea = "  ".join(partes)
    extra = []
    if isinstance(req, dict):
        h = req.get("headers") or {}
        if "Idempotency-Key" in h:
            extra.append(f"idempotency-key: {h['Idempotency-Key']}")
    if isinstance(resp, dict) and isinstance(resp.get("body"), dict):
        b = resp["body"]
        det = b.get("detail") if isinstance(b.get("detail"), dict) else b
        if isinstance(det, dict) and det.get("error"):
            extra.append(f"error: {det['error']}")
    out = [linea] + [f"        {e}" for e in extra]
    if full:
        out.append("        " + json.dumps(d, ensure_ascii=False, indent=2).replace("\n", "\n        "))
    return out


def fmt_evento(tag, d, full):
    if tag == "TRACE_JSON":
        return fmt_trace(d, full)
    if tag == "APP_LOG_JSON":
        det = d.get("details") or {}
        det_s = ", ".join(f"{k}={corto(v, 40)}" for k, v in det.items())
        return [f"  LOG {d.get('level','?'):<7} {d.get('event','?')}  {det_s}"]
    if tag == "SAGA_EVENT_JSON":
        return [f"  SAGA #{d.get('sequence')} {d.get('step')} {d.get('status')}: {d.get('detail')}"]
    return []  # EVIDENCE se imprime aparte


def evidencia(d):
    d = {k: v for k, v in d.items() if k not in ("experiment", "saga_events", "log_events")}
    return json.dumps(d, ensure_ascii=False, indent=2)


def render(cabecera, tests, resumen, md, full):
    H = (lambda n, t: f"{'#'*n} {t}") if md else (lambda n, t: f"\n{t}\n{('=' if n<3 else '-')*len(t)}")
    out = [H(1, "Reporte de experimentos")]
    out += [l for l in cabecera if l.strip()][:6]
    tot = {}
    for t in tests:
        tot[t["resultado"]] = tot.get(t["resultado"], 0) + 1
    out += ["", "Resumen: " + ", ".join(f"{v} {k}" for k, v in tot.items())]
    out += [""] + [f"  {'✔' if t['resultado']=='PASSED' else '✘'} {t['nombre'].split('::')[-1]}" for t in tests]

    for t in tests:
        exp = next((d.get("experiment") for _, d in t["eventos"] if d.get("experiment")), "")
        out.append(H(2, f"{'✔' if t['resultado']=='PASSED' else '✘'} {t['nombre']}"))
        out.append(f"Experimento: {exp}   Resultado: {t['resultado']}")
        if md: out.append("\n```")
        for tag, d in t["eventos"]:
            if tag != "EVIDENCE_JSON":
                out += fmt_evento(tag, d, full)
        if md: out.append("```")
        for tag, d in t["eventos"]:
            if tag == "EVIDENCE_JSON":
                out += ["", "Evidencia:", "```json" if md else "", evidencia(d), "```" if md else ""]
        out += t["otras"]
    if resumen:
        out += [H(2, "Resumen de pytest")] + resumen
    return "\n".join(out) + "\n"


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        sys.exit(__doc__)
    md, full = "--md" in sys.argv, "--full" in sys.argv
    src = Path(args[0])
    cab, tests, res = parsear(leer(src))
    dst = src.with_suffix(".legible." + ("md" if md else "txt"))
    dst.write_text(render(cab, tests, res, md, full), encoding="utf-8")
    print(f"OK -> {dst}  ({len(tests)} tests)")


if __name__ == "__main__":
    main()
