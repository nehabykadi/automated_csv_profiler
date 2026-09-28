"""
Profile a single-table CSV and write a markdown report, plots and a JSON summary.

    python src/profiler.py data/my_data.csv                # writes to output/my_data/
    python src/profiler.py data/my_data.csv -o results --model llama3.2
    python src/profiler.py data/my_data.csv --no-llm

Needs: pandas, numpy, matplotlib. For the AI section, a running Ollama server
(`ollama pull llama3.2` first). Nothing in the CSV is modified or dropped.
"""
import argparse
import json
import re
import urllib.request
import warnings
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

warnings.filterwarnings("ignore", message="Could not infer format")

MISSING = {"", "na", "n/a", "nan", "null", "none", "?", "-", "--"}
BOOL_SETS = [{"true", "false"}, {"t", "f"}, {"yes", "no"}, {"y", "n"}, {"0", "1"}]
ID_WORDS = {"id", "uuid", "guid", "key", "index", "idx"}
# Needs a recognisable date shape so plain text like "May" is not called a date.
DATE_SHAPE = r"\d{1,4}\s*[-/.]\s*\d{1,2}\s*[-/.]\s*\d{1,4}|\d{4}-\d{2}"

HIGH_MISSING_PCT = 30      # flag columns missing more than this share
HIGH_CARDINALITY = 50      # categorical columns with more unique values than this get flagged
TOP_N = 10
MAX_PLOTS = 8
MAX_CAT_SECTIONS = 20

SENSITIVE_WORDS = {"ssn", "social", "password", "passwd", "dob", "birth", "birthday", "address",
                   "phone", "email", "salary", "income", "credit", "card", "passport", "license",
                   "gender", "sex", "race", "ethnicity", "religion", "diagnosis"}
PERSON_WORDS = {"first", "last", "full", "middle", "given", "sur", "patient", "customer",
                "user", "person", "employee"}
VALUE_PATTERNS = {
    "email addresses": r"^[^@\s]+@[^@\s]+\.\w+$",
    "SSN-style numbers": r"^\d{3}-\d{2}-\d{4}$",
    "phone-style numbers": r"^\+?\d?[\s.-]?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}$",
    "card-style numbers": r"^\d{4}[\s-]?\d{4}[\s-]?\d{4}[\s-]?\d{4}$",
}

DEFAULT_MODEL = "llama3.2"
DEFAULT_HOST = "http://localhost:11434"

SYSTEM_PROMPT = """You write insights for a data profiling report. You receive a JSON summary that Python already computed.
Rules:
- Use only facts that appear in the summary. Do not calculate new statistics.
- Do not guess what a column means or what its units are.
- Never say correlation shows causation, and never call the data clean or safe.
- If the summary lacks information for something, say so instead of filling the gap.
Return JSON only, shaped like:
{"insights": [{"type": "...", "column": "...", "insight": "..."}]}
Give 5 to 8 insights. "type" is one of: data_quality, distribution, categorical, relationship, limitation, follow_up_question.
Cover data quality, distribution, categorical (only if categorical_columns is not empty), relationship (only if relationships has pairs),
at least one limitation and at least one follow_up_question. Every quantitative insight must name the column and quote the
exact count, percentage or statistic from the summary."""


# ---------- column typing ----------

def tokens(name):
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", name).lower()
    return [t for t in re.split(r"[^a-z0-9]+", spaced) if t]


def is_id_name(name):
    return bool(set(tokens(name)) & ID_WORDS) or name.lower().endswith("id")


def infer_column(name, s):
    """s = column as stripped strings with missing values set to NaN.
    Returns (technical type, probable role)."""
    v = s.dropna()
    n = len(v)
    if n == 0:
        return "empty", "Unknown or mixed type"
    unique = v.nunique()
    ratio = unique / n

    # Boolean
    vals = set(v.str.lower().unique())
    if len(vals) <= 2 and any(vals <= b for b in BOOL_SETS) and (vals != {"0"} and vals != {"1"}):
        return "boolean", "Boolean field"

    # Numeric (>=95% of values parse as numbers; leading zeros like "01234" are codes, not numbers)
    nums = pd.to_numeric(v, errors="coerce")
    share = nums.notna().mean()
    has_leading_zero = v.str.match(r"^0\d+$").mean() >= 0.05
    if share >= 0.95 and not has_leading_zero:
        is_int = bool((nums.dropna() % 1 == 0).all())
        if is_int and ((is_id_name(name) and ratio >= 0.5) or (n >= 20 and ratio >= 0.99 and nums.dropna().is_monotonic_increasing)):
            return "int64", "Identifier-like field"
        return ("int64" if is_int else "float64"), "Numeric measure"
    if 0.05 <= share < 0.95:
        return "mixed (numeric + text)", "Unknown or mixed type"

    # Date-like
    if v.str.contains(DATE_SHAPE, regex=True).mean() >= 0.8:
        parsed = pd.to_datetime(v, errors="coerce")
        if parsed.notna().mean() >= 0.9:
            return "datetime", "Date-like field"
        return "mixed (date-like + text)", "Unknown or mixed type"

    # Text: free text, identifier, or categorical
    avg_words = v.str.split().str.len().mean()
    avg_len = v.str.len().mean()
    if avg_words >= 6 or avg_len >= 60:
        return "string", "Free-text field"
    if (is_id_name(name) and ratio >= 0.5) or (n >= 20 and ratio >= 0.95):
        return "string", "Identifier-like field"
    return "string", "Categorical attribute"


def load(path):
    # Read as text so nothing is silently converted.
    raw = pd.read_csv(path, dtype=str, keep_default_na=False, encoding_errors="replace")
    df = raw.apply(lambda col: col.str.strip())
    return df.mask(df.apply(lambda col: col.str.lower().isin(MISSING)))


# ---------- statistics ----------

def r(x, digits=4):
    return None if pd.isna(x) else round(float(x), digits)


def numeric_stats(x, n_rows):
    v = x.dropna()
    q1, q3 = v.quantile(0.25), v.quantile(0.75)
    iqr = q3 - q1
    outliers = int(((v < q1 - 1.5 * iqr) | (v > q3 + 1.5 * iqr)).sum())
    counts = v.value_counts()
    top = list(counts[counts == counts.iloc[0]].index)
    mode = [r(m) for m in sorted(top)] if counts.iloc[0] > 1 and len(top) <= 3 else None
    st = {
        "valid_count": len(v), "missing_count": n_rows - len(v),
        "missing_pct": r(100 * (n_rows - len(v)) / n_rows, 2),
        "min": r(v.min()), "max": r(v.max()), "mean": r(v.mean()), "median": r(v.median()),
        "mode": mode, "std": r(v.std()), "q1": r(q1), "q3": r(q3), "iqr": r(iqr),
        "outlier_count": outliers, "outlier_pct": r(100 * outliers / len(v), 2),
    }
    if iqr == 0:
        st["note"] = "IQR is 0, so the 1.5*IQR rule flags every value that differs from the quartiles."
    return st


def categorical_stats(s):
    vc = s.dropna().value_counts()
    total = int(vc.sum())
    return {
        "unique": int(vc.size),
        "most_frequent": [str(k) for k in vc[vc == vc.iloc[0]].index[:5]],
        "top": [{"value": str(k), "count": int(c), "pct": round(100 * c / total, 2)}
                for k, c in vc.head(TOP_N).items()],
    }


def relationships(num):
    cols = [c for c, s in num.items() if s.nunique() > 1]
    if len(cols) < 2:
        return None, [], "needs at least two numeric measure columns with more than one distinct value"
    corr = pd.DataFrame({c: num[c] for c in cols}).corr(min_periods=3)
    pairs = []
    for i, a in enumerate(cols):
        for b in cols[i + 1:]:
            if pd.notna(corr.loc[a, b]):
                pairs.append({"a": a, "b": b, "r": round(float(corr.loc[a, b]), 3),
                              "n": int((num[a].notna() & num[b].notna()).sum())})
    if not pairs:
        return None, [], "numeric columns do not share enough rows (need at least 3) to compute correlations"
    return corr, pairs, None


def sensitive_hits(name, s):
    hits = []
    t = set(tokens(name))
    if t & SENSITIVE_WORDS or ("name" in t and t & PERSON_WORDS):
        hits.append("column name")
    sample = s.dropna().head(1000)
    if len(sample):
        for label, pat in VALUE_PATTERNS.items():
            if sample.str.match(pat).mean() >= 0.2:
                hits.append(f"values resemble {label}")
    return hits


# ---------- plots ----------

def make_plots(out, df, info, typed, num_cols, cat_cols, date_cols, corr, pairs):
    files, skipped = [], {}
    short = lambda s: str(s) if len(str(s)) <= 30 else str(s)[:27] + "..."
    slug = lambda s: re.sub(r"\W+", "_", str(s))[:40]
    usable = sorted([c for c in num_cols if info[c]["unique"] > 1], key=lambda c: -info[c]["non_missing"])

    def missing():
        m = pd.Series({c: i["missing_pct"] for c, i in info.items() if i["missing_pct"] > 0}, dtype=float)
        if m.empty:
            return "no missing values"
        m = m.sort_values().tail(30)
        fig, ax = plt.subplots(figsize=(7, 0.35 * len(m) + 1.5))
        ax.barh([short(c) for c in m.index], m.values)
        ax.set(title="Missing values by column", xlabel="Missing (%)", ylabel="Column")
        return [(fig, "missing_values.png")]

    def heatmap():
        if corr is None:
            return "fewer than two usable numeric columns"
        cols = sorted(corr.columns, key=lambda c: -info[c]["non_missing"])[:15]
        m = corr.loc[cols, cols]
        k = len(cols)
        fig, ax = plt.subplots(figsize=(0.6 * k + 3, 0.6 * k + 2.5))
        im = ax.imshow(m, cmap="coolwarm", vmin=-1, vmax=1)
        labels = [short(c) for c in cols]
        ax.set_xticks(range(k))
        ax.set_xticklabels(labels, rotation=45, ha="right")
        ax.set_yticks(range(k))
        ax.set_yticklabels(labels)
        if k <= 10:
            for i in range(k):
                for j in range(k):
                    ax.text(j, i, f"{m.iloc[i, j]:.2f}", ha="center", va="center", fontsize=8)
        fig.colorbar(im, label="Pearson r")
        ax.set(title="Correlation heatmap (numeric columns)", xlabel="Column", ylabel="Column")
        return [(fig, "correlation_heatmap.png")]

    def scatter():
        if not pairs:
            return "no numeric pair with a computable correlation"
        p = max(pairs, key=lambda p: abs(p["r"]))
        d = pd.DataFrame({p["a"]: typed[p["a"]], p["b"]: typed[p["b"]]}).dropna()
        d = d.sample(min(len(d), 5000), random_state=0)
        fig, ax = plt.subplots(figsize=(6, 5))
        ax.scatter(d[p["a"]], d[p["b"]], s=10, alpha=0.5)
        ax.set(title=f"{p['b']} vs {p['a']} (r = {p['r']:.2f})", xlabel=p["a"], ylabel=p["b"])
        return [(fig, "scatter_top_pair.png")]

    def time_series():
        for c in date_cols:
            d = typed[c].dropna()
            if d.nunique() < 3:
                continue
            key = d.dt.to_period("M" if (d.max() - d.min()).days > 60 else "D")
            if usable:
                y = typed[usable[0]][d.index].groupby(key).mean()
                ylabel = f"Mean of {usable[0]}"
            else:
                y = d.groupby(key).size()
                ylabel = "Rows"
            if len(y) < 3:
                continue
            fig, ax = plt.subplots(figsize=(8, 4))
            ax.plot(y.index.to_timestamp(), y.values, marker="o")
            ax.set(title=f"{ylabel} over {c}", xlabel=c, ylabel=ylabel)
            fig.autofmt_xdate()
            return [(fig, "time_series.png")]
        return "no date-like column with at least 3 distinct dates spread over 3 or more periods"

    def hists():
        if not usable:
            return "no numeric column with more than one distinct value"
        res = []
        for c in usable[:2]:
            fig, ax = plt.subplots(figsize=(6, 4))
            ax.hist(typed[c].dropna(), bins=30)
            ax.set(title=f"Distribution of {c}", xlabel=c, ylabel="Count")
            res.append((fig, f"hist_{slug(c)}.png"))
        return res

    def box():
        groups = [c for c in cat_cols if 2 <= info[c]["unique"] <= 8]
        if not usable or not groups:
            return "needs a numeric column and a categorical column with 2 to 8 categories"
        n, g = usable[0], groups[0]
        d = pd.DataFrame({"v": typed[n], "g": df[g]}).dropna()
        if d.empty:
            return "numeric and categorical columns never have values in the same row"
        labels = sorted(d["g"].unique())
        fig, ax = plt.subplots(figsize=(7, 4.5))
        ax.boxplot([d.loc[d["g"] == l, "v"] for l in labels])
        ax.set_xticks(range(1, len(labels) + 1))
        ax.set_xticklabels([short(l) for l in labels], rotation=30, ha="right")
        ax.set(title=f"{n} by {g}", xlabel=g, ylabel=n)
        return [(fig, f"box_{slug(n)}_by_{slug(g)}.png")]

    def cats():
        picks = [c for c in cat_cols if info[c]["unique"] >= 2][:2]
        if not picks:
            return "no categorical column with at least 2 categories"
        res = []
        for c in picks:
            vc = df[c].value_counts().head(TOP_N).sort_values()
            fig, ax = plt.subplots(figsize=(7, 0.4 * len(vc) + 1.5))
            ax.barh([short(k) for k in vc.index], vc.values)
            ax.set(title=f"Most frequent categories in {c}", xlabel="Count", ylabel=c)
            res.append((fig, f"categories_{slug(c)}.png"))
        return res

    plan = [("missing-value bar chart", missing), ("correlation heatmap", heatmap),
            ("scatterplot", scatter), ("time-series plot", time_series), ("histogram", hists),
            ("numeric distribution by category (boxplot)", box), ("categorical frequency bar chart", cats)]
    for kind, build in plan:
        result = build()
        if isinstance(result, str):
            skipped[kind] = result
            continue
        for fig, name in result:
            if len(files) >= MAX_PLOTS:
                plt.close(fig)
                skipped[kind] = f"limit of {MAX_PLOTS} plots reached (some not drawn)"
                continue
            name = f"plot_{len(files) + 1:02d}.png"
            title = fig.axes[0].get_title()
            fig.tight_layout()
            fig.savefig(out / name, dpi=110)
            plt.close(fig)
            files.append((title, name))
    return files, skipped


# ---------- Ollama ----------

def ask_ollama(summary_text, model, host):
    body = json.dumps({
        "model": model, "stream": False, "format": "json",
        "options": {"temperature": 0.2, "num_ctx": 8192},  # default context is too small for the summary
        "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                     {"role": "user", "content": summary_text}],
    }).encode()
    req = urllib.request.Request(f"{host}/api/chat", body, {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as resp:
        return json.load(resp)["message"]["content"]


def parse_insights(raw):
    insights = json.loads(raw)["insights"]
    if not isinstance(insights, list) or not insights:
        raise ValueError("model returned no insights")
    return insights[:8]


def unverified_numbers(text, summary_text):
    """Numbers in an insight that do not appear (within rounding) in the Python summary."""
    pattern = r"\d+(?:\.\d+)?"
    have = [float(x) for x in re.findall(pattern, summary_text.replace(",", ""))]
    bad = []
    for m in re.findall(pattern, text.replace(",", "")):
        x = float(m)
        if not any(abs(x - h) <= max(0.051, abs(h) * 0.005) for h in have):
            bad.append(m)
    return bad


# ---------- report helpers ----------

def fmt(x):
    return "–" if x is None else f"{x:,.4f}".rstrip("0").rstrip(".")


def table(headers, rows):
    esc = lambda x: str(x).replace("|", "\\|")
    lines = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    return lines + ["| " + " | ".join(esc(x) for x in row) + " |" for row in rows]


def generate_profile(csv_path, output_dir="output", use_llm=True, model=DEFAULT_MODEL, host=DEFAULT_HOST):
    """Single entry point: read a CSV and write everything to <output_dir>/<csv name>/."""
    csv_path = Path(csv_path)
    out = Path(output_dir) / re.sub(r"\W+", "_", csv_path.stem).strip("_").lower()
    plots_dir = out / "plots"
    plots_dir.mkdir(parents=True, exist_ok=True)
    df = load(csv_path)
    n_rows, n_cols = df.shape
    if n_rows == 0:
        raise ValueError("The file has a header but no data rows, so there is nothing to profile.")

    # column overview + typed copies of numeric/date columns
    info, typed = {}, {}
    for c in df.columns:
        t, role = infer_column(c, df[c])
        nm = int(df[c].notna().sum())
        info[c] = {"type": t, "role": role, "non_missing": nm,
                   "missing_pct": round(100 * (n_rows - nm) / n_rows, 2), "unique": int(df[c].nunique())}
        if role == "Numeric measure":
            typed[c] = pd.to_numeric(df[c], errors="coerce")
        elif role == "Date-like field":
            typed[c] = pd.to_datetime(df[c], errors="coerce")
    num_cols = [c for c in df.columns if info[c]["role"] == "Numeric measure"]
    date_cols = [c for c in df.columns if info[c]["role"] == "Date-like field"]
    cat_cols = [c for c in df.columns if info[c]["role"] in ("Categorical attribute", "Boolean field")]
    overview = pd.DataFrame([{"Column": c, "Inferred type": i["type"], "Probable role": i["role"],
                              "Non-missing": i["non_missing"], "Missing %": i["missing_pct"],
                              "Unique values": i["unique"]} for c, i in info.items()])
    overview.to_csv(out / "column_profile.csv", index=False)

    # data quality
    findings = []
    flag = lambda check, col, detail: findings.append({"check": check, "column": col, "detail": detail})
    dups = int(df.duplicated().sum())
    if dups:
        flag("duplicate rows", None, f"{dups:,} of {n_rows:,} rows ({100 * dups / n_rows:.2f}%) are exact duplicates")
    for c, i in info.items():
        if i["unique"] == 1:
            flag("single value", c, f"only one distinct value across {i['non_missing']:,} non-missing rows")
        if i["missing_pct"] > HIGH_MISSING_PCT:
            flag("high missingness", c, f"{i['missing_pct']}% missing ({n_rows - i['non_missing']:,} of {n_rows:,} rows)")
        if i["type"].startswith("mixed"):
            flag("mixed types", c, f"{i['type']}; values could not be read as one type")
        elif c in typed:
            bad = i["non_missing"] - int(typed[c].notna().sum())
            if bad:
                flag("mixed types", c, f"{bad:,} non-missing values could not be parsed as {i['type']}")
        if i["role"] == "Identifier-like field":
            flag("identifier-like", c, f"{i['unique']:,} unique values; excluded from correlations")
        elif i["role"] == "Categorical attribute" and i["unique"] > HIGH_CARDINALITY:
            flag("high cardinality", c, f"{i['unique']:,} distinct categories")
        hits = sensitive_hits(c, df[c])
        if hits:
            flag("possibly sensitive", c, "; ".join(hits))

    # statistics and relationships
    num_stats = {c: numeric_stats(typed[c], n_rows) for c in num_cols}
    cat_stats = {c: categorical_stats(df[c]) for c in cat_cols}
    corr, pairs, rel_reason = relationships({c: typed[c] for c in num_cols})
    pos = sorted([p for p in pairs if p["r"] > 0], key=lambda p: -p["r"])[:3]
    neg = sorted([p for p in pairs if p["r"] < 0], key=lambda p: p["r"])[:3]
    if corr is not None:
        corr.round(3).to_csv(out / "correlation_matrix.csv")

    plots, plots_skipped = make_plots(plots_dir, df, info, typed, num_cols, cat_cols, date_cols, corr, pairs)

    analyses_skipped = {}
    if not num_cols:
        analyses_skipped["numeric statistics"] = "no numeric measure columns"
    if not cat_cols:
        analyses_skipped["categorical statistics"] = "no categorical or boolean columns"
    if rel_reason:
        analyses_skipped["relationships"] = rel_reason

    summary = {
        "dataset": {"filename": csv_path.name, "rows": n_rows, "columns": n_cols},
        "columns": [{"name": c, **i} for c, i in info.items()],
        "data_quality": {"duplicate_rows": dups, "high_missing_threshold_pct": HIGH_MISSING_PCT,
                         "findings": findings,
                         "note": "Sensitive-data flags are heuristics; no flag does not mean the data is safe."},
        "numeric_columns": num_stats,
        "categorical_columns": {c: {"unique": s["unique"], "top": s["top"][:5]} for c, s in cat_stats.items()},
        "relationships": ({"strongest_positive": pos, "strongest_negative": neg,
                           "note": "Pearson r on rows where both values exist; not causal."}
                          if corr is not None else {"skipped": rel_reason}),
        "analyses_skipped": analyses_skipped,
        "plots_skipped": plots_skipped,
    }
    summary_text = json.dumps(summary, default=str)
    if len(summary_text) > 24000:  # keep it inside the model's context window
        summary.pop("columns")
        summary["note"] = "Per-column overview omitted for size; see column_profile.csv."
        summary_text = json.dumps(summary, default=str)
    (out / "analysis_summary.json").write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")

    # AI narrative (Python numbers only, model just writes them up)
    insights, llm_note = [], None
    (out / "llm_prompt.txt").write_text(f"{SYSTEM_PROMPT}\n\n--- VERIFIED SUMMARY (JSON) ---\n{summary_text}", encoding="utf-8")
    if not use_llm:
        llm_note = "AI-generated narrative insights were skipped (LLM disabled)."
        (out / "llm_response.txt").write_text(llm_note, encoding="utf-8")
    else:
        raw = ""
        try:
            raw = ask_ollama(summary_text, model, host)
            insights = parse_insights(raw)
        except Exception as e:
            llm_note = f"AI-generated narrative insights were skipped because the model was unavailable or its reply was unusable ({e})."
            raw = raw or llm_note
        (out / "llm_response.txt").write_text(raw, encoding="utf-8")

    # report
    L = [f"# Profile: {csv_path.name}", "", "## 1. Dataset overview", "",
         f"- **Filename:** {csv_path.name}", f"- **Rows:** {n_rows:,}", f"- **Columns:** {n_cols}",
         f"- **Column names:** {', '.join(df.columns)}", ""]
    L += table(["Column", "Inferred type", "Probable role", "Non-missing", "Missing %", "Unique values"],
               [(c, i["type"], i["role"], f"{i['non_missing']:,}", f"{i['missing_pct']:.2f}%", f"{i['unique']:,}")
                for c, i in info.items()])
    L += ["", "*Types and roles are heuristics based on the values. The program does not guess what any column name "
          "or abbreviation means, and cannot know units or valid ranges. Blank values and tokens like NA, null, ? "
          "count as missing; the file itself is not changed.*", "", "## 2. Data quality", "",
          f"Checks run: duplicate rows, single-value columns, missingness above {HIGH_MISSING_PCT}%, mixed types, "
          "identifier-like or high-cardinality columns, possibly sensitive fields. Nothing was removed or altered.", ""]
    if findings:
        L += table(["Check", "Column", "Detail"], [(f["check"], f["column"] or "(whole file)", f["detail"]) for f in findings])
    else:
        L.append("No issues were flagged by these checks.")
    L += ["", "*Sensitive-field detection only looks at column names and simple value patterns. "
          "No warning does not mean the dataset is free of sensitive data.*", "", "## 3. Numeric columns", ""]
    if num_cols:
        L += table(["Column", "Valid", "Missing", "Min", "Max", "Mean", "Median", "Mode", "Std", "Q1", "Q3", "IQR", "Outliers (1.5×IQR)"],
                   [(c, f"{s['valid_count']:,}", f"{s['missing_count']:,} ({s['missing_pct']}%)", fmt(s["min"]), fmt(s["max"]),
                     fmt(s["mean"]), fmt(s["median"]), ", ".join(fmt(m) for m in s["mode"]) if s["mode"] else "none",
                     fmt(s["std"]), fmt(s["q1"]), fmt(s["q3"]), fmt(s["iqr"]),
                     f"{s['outlier_count']:,} ({s['outlier_pct']}%)") for c, s in num_stats.items()])
        L += ["", "*Outliers are flagged, not removed. Mode is shown only when a value repeats and there are at most 3 tied.*"]
    else:
        L.append("Skipped: no numeric measure columns.")
    L += ["", "## 4. Categorical columns", ""]
    if cat_cols:
        for c in list(cat_stats)[:MAX_CAT_SECTIONS]:
            s = cat_stats[c]
            L += [f"**{c}**: {s['unique']:,} unique; most frequent: {', '.join(s['most_frequent'])}", ""]
            L += table(["Value", "Count", "% of non-missing"], [(t["value"], f"{t['count']:,}", f"{t['pct']}%") for t in s["top"]])
            L.append("")
        if len(cat_stats) > MAX_CAT_SECTIONS:
            L.append(f"*{len(cat_stats) - MAX_CAT_SECTIONS} more categorical columns not shown; see analysis_summary.json.*")
    else:
        L.append("Skipped: no categorical or boolean columns.")
    L += ["", "## 5. Relationships", ""]
    if corr is None:
        L.append(f"Skipped: {rel_reason}.")
    else:
        L += ["Pearson correlation, identifier-like columns excluded. Correlation is not causation. "
              "Full matrix in `correlation_matrix.csv`.", ""]
        for title, group in (("Strongest positive", pos), ("Strongest negative", neg)):
            L.append(f"**{title}:**" + ("" if group else " none"))
            if group:
                L += [""] + table(["Column A", "Column B", "r", "Rows used"], [(p["a"], p["b"], p["r"], f"{p['n']:,}") for p in group])
            L.append("")
    L += ["## 6. Visualizations", ""]
    for title, name in plots:
        L += [f"![{title}](plots/{name})", ""]
    if not plots:
        L.append("No plots could be generated.")
    if plots_skipped:
        L += ["**Skipped plot types:**", ""] + [f"- {k}: {v}" for k, v in plots_skipped.items()] + [""]
    L += ["## 7. AI-assisted insights", ""]
    if llm_note:
        L.append(llm_note)
    else:
        L.append(f"*Written by `{model}` from the verified summary in `analysis_summary.json`. The model did not compute anything.*")
        L.append("")
        for k, item in enumerate(insights, 1):
            text = str(item.get("insight", ""))
            bad = unverified_numbers(text, summary_text)
            warn = f" **(numbers not found in the summary: {', '.join(bad)})**" if bad else ""
            col = f" [{item['column']}]" if item.get("column") else ""
            L.append(f"{k}. **{str(item.get('type', 'insight')).replace('_', ' ')}**{col}: {text}{warn}")
        if len(insights) < 5:
            L += ["", "*The model returned fewer than five insights.*"]

    (out / "report.md").write_text("\n".join(L), encoding="utf-8")
    print(f"Report written to {out / 'report.md'}")
    return summary


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("csv_path")
    ap.add_argument("-o", "--output-dir", default="output")
    ap.add_argument("--no-llm", action="store_true", help="skip the Ollama narrative")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--host", default=DEFAULT_HOST)
    a = ap.parse_args()
    generate_profile(a.csv_path, a.output_dir, use_llm=not a.no_llm, model=a.model, host=a.host)
