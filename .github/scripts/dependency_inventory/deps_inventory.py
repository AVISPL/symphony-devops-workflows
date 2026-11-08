#!/usr/bin/env python3
"""Third-party dependency inventory across Symphony microservices.

Reads a JSON list of repos, fetches each repo's pom.xml from a GitHub org via the
REST API, extracts explicit third-party (non-com.avispl) <dependencies>, attributes
a version source for BOM-managed deps, enriches each distinct artifact with
license + origin from Maven Central, optionally cross-checks against an approved
3rd-party Confluence page, and writes markdown + csv + (optionally) Allure results.

stdlib only. Auth:
  GitHub   : token in $GH_TOKEN or $GITHUB_TOKEN (PAT with read access to the org).
  Confluence (optional): $CONFLUENCE_BASE_URL, $CONFLUENCE_EMAIL, $CONFLUENCE_API_TOKEN.
"""
import argparse
import base64
import csv
import json
import os
import re
import sys
import time
import uuid
import urllib.request
import urllib.parse
import urllib.error
import xml.etree.ElementTree as ET

POM_NS = "{http://maven.apache.org/POM/4.0.0}"
GH_API = "https://api.github.com"

# Repos with no Maven pom (non-Java / not found). Listed as N/A in the report.
NON_JAVA = {
    "symphony-ui-react": "React frontend (package.json, no pom)",
    "symphony-rest-docs": "Node / Redoc viewer (no pom)",
    "symphony-camunda-modeler": "JS / eslint frontend (no pom)",
    "symphony-outlook-plugin": "Office add-in under SymphonyAddIn/ (no Maven pom)",
    "symphony-versions-aggregato": "repo not found on GitHub (name truncated?)",
}

# Spring Boot parent version used for spring-boot-dependencies BOM lookup. Best-effort
# fallback; the real value is read from symphony-shared-parent at runtime when present.
SPRING_BOOT_FALLBACK = "3.5.15"


# --------------------------- HTTP helpers ---------------------------

def _gh_token():
    return os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")


def http_get(url, headers=None, auth=None):
    req = urllib.request.Request(url, headers=headers or {})
    if auth:
        token = base64.b64encode(f"{auth[0]}:{auth[1]}".encode()).decode()
        req.add_header("Authorization", f"Basic {token}")
    with urllib.request.urlopen(req, timeout=40) as r:
        return r.read().decode("utf-8", "replace")


def gh_get(path, raw=False):
    """GET the GitHub REST API. Returns parsed JSON, or None on 404/error."""
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "deps-inventory"}
    tok = _gh_token()
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    try:
        body = http_get(GH_API + path, headers)
    except urllib.error.HTTPError as e:
        if e.code in (403, 429):
            # secondary rate limit — back off once and retry
            time.sleep(20)
            try:
                body = http_get(GH_API + path, headers)
            except Exception:
                return None
        return None
    except Exception:
        return None
    if raw:
        return body
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return None


def fetch_pom(org, repo, ref, path="pom.xml"):
    data = gh_get(f"/repos/{org}/{repo}/contents/{urllib.parse.quote(path)}?ref={urllib.parse.quote(ref)}")
    if not data or "content" not in data:
        return None
    try:
        return base64.b64decode(data["content"]).decode("utf-8", "replace")
    except Exception:
        return None


def resolve_ref(org, repo, prefer):
    """Prefer the given ref (e.g. release/6.27); else the highest release/x.y; else default."""
    branches, page = [], 1
    while True:
        b = gh_get(f"/repos/{org}/{repo}/branches?per_page=100&page={page}")
        if not b:
            break
        branches += [x["name"] for x in b]
        if len(b) < 100:
            break
        page += 1
    if prefer and prefer in branches:
        return prefer
    rels = []
    for name in branches:
        m = re.fullmatch(r"release/(\d+)\.(\d+)", name)
        if m:
            rels.append(((int(m.group(1)), int(m.group(2))), name))
    if rels:
        return max(rels)[1]
    info = gh_get(f"/repos/{org}/{repo}")
    return info.get("default_branch", "develop") if info else "develop"


# --------------------------- POM parsing ---------------------------

def fel(el, name):
    """Child by name, namespaced first then namespace-less; el or None."""
    c = el.find(POM_NS + name)
    return c if c is not None else el.find(name)


def localname(tag):
    return tag.split("}")[-1]


def text(el, child):
    c = fel(el, child)
    return c.text.strip() if c is not None and c.text else None


def parse_properties(root):
    props = {}
    p = fel(root, "properties")
    if p is not None:
        for child in p:
            if child.text:
                props[localname(child.tag)] = child.text.strip()
    return props


def resolve_prop(value, props):
    if not value:
        return value
    m = re.fullmatch(r"\$\{([^}]+)\}", value.strip())
    if m and m.group(1) in props:
        return props[m.group(1)]
    return value


def iter_direct_dependencies(root):
    deps_el = fel(root, "dependencies")
    if deps_el is None:
        return
    for dep in deps_el.findall(POM_NS + "dependency") + deps_el.findall("dependency"):
        g, a, v, s = (text(dep, "groupId"), text(dep, "artifactId"),
                      text(dep, "version"), text(dep, "scope"))
        if g and a:
            yield g, a, v, s


def managed_ga_set(pom_text):
    out = set()
    if not pom_text:
        return out
    try:
        root = ET.fromstring(pom_text)
    except ET.ParseError:
        return out
    dm = fel(root, "dependencyManagement")
    if dm is None:
        return out
    deps = fel(dm, "dependencies")
    if deps is None:
        return out
    for dep in deps.findall(POM_NS + "dependency") + deps.findall("dependency"):
        g, a = text(dep, "groupId"), text(dep, "artifactId")
        if g and a:
            out.add(f"{g}:{a}")
    return out


# --------------------------- Maven Central enrichment ---------------------------

def mc_latest_version(g, a):
    path = g.replace(".", "/")
    try:
        meta = http_get(f"https://repo1.maven.org/maven2/{path}/{a}/maven-metadata.xml",
                        {"User-Agent": "deps-inventory"})
        m = re.search(r"<release>([^<]+)</release>", meta) or re.search(r"<latest>([^<]+)</latest>", meta)
        if m:
            return m.group(1)
        vers = re.findall(r"<version>([^<]+)</version>", meta)
        if vers:
            return vers[-1]
    except Exception:
        pass
    url = "https://search.maven.org/solrsearch/select?" + urllib.parse.urlencode(
        {"q": f'g:"{g}" AND a:"{a}"', "rows": 1, "wt": "json"})
    try:
        docs = json.loads(http_get(url, {"User-Agent": "deps-inventory"})).get("response", {}).get("docs", [])
        if docs:
            return docs[0].get("latestVersion") or docs[0].get("v")
    except Exception:
        return None
    return None


def mc_pom(g, a, v):
    path = g.replace(".", "/")
    try:
        return http_get(f"https://repo1.maven.org/maven2/{path}/{a}/{v}/{a}-{v}.pom",
                        {"User-Agent": "deps-inventory"})
    except Exception:
        return None


def extract_license_origin(pom_text, depth=0):
    if not pom_text or depth > 3:
        return None, None, None
    try:
        root = ET.fromstring(pom_text)
    except ET.ParseError:
        return None, None, None
    origin = text(root, "url")
    lic_name = lic_url = None
    lic_block = fel(root, "licenses")
    if lic_block is not None:
        first = fel(lic_block, "license")
        if first is not None:
            lic_name, lic_url = text(first, "name"), text(first, "url")
    scm = fel(root, "scm")
    if not origin and scm is not None:
        origin = text(scm, "url")
    if not lic_name:
        parent = fel(root, "parent")
        if parent is not None:
            pg, pa, pv = text(parent, "groupId"), text(parent, "artifactId"), text(parent, "version")
            if pg and pa and pv:
                p_o, p_n, p_u = extract_license_origin(mc_pom(pg, pa, pv), depth + 1)
                lic_name, lic_url, origin = lic_name or p_n, lic_url or p_u, origin or p_o
    return origin, lic_name, lic_url


SPDX = [
    (r"apache.*2", "Apache-2.0"), (r"\bmit\b", "MIT"),
    (r"bsd.*3|3.*clause", "BSD-3-Clause"), (r"bsd.*2|2.*clause", "BSD-2-Clause"), (r"\bbsd\b", "BSD"),
    (r"eclipse public license.*2|epl.*2", "EPL-2.0"), (r"eclipse public license|epl.*1", "EPL-1.0"),
    (r"eclipse distribution|edl", "EDL-1.0"),
    (r"lesser.*3|lgpl.*3", "LGPL-3.0"), (r"lesser.*2|lgpl.*2", "LGPL-2.1"),
    (r"gnu general.*2|gpl.*2", "GPL-2.0"), (r"\bgpl\b", "GPL"),
    (r"mozilla.*2|mpl.*2", "MPL-2.0"), (r"cddl", "CDDL"), (r"public domain", "Public-Domain"),
    (r"\bisc\b", "ISC"), (r"bouncy", "Bouncy-Castle"),
]


def normalize_license(name):
    if not name:
        return None
    low = name.lower()
    for pat, spdx in SPDX:
        if re.search(pat, low):
            return spdx
    return name.strip()


def enrich(g, a, cache):
    key = f"{g}:{a}"
    if key in cache:
        return cache[key]
    res = {"origin": None, "license_url": None, "license_name": None, "license_type": None, "note": ""}
    v = mc_latest_version(g, a)
    if not v:
        res["note"] = "not on Maven Central (internal Nexus / relocated)"
        res["license_type"] = res["note"]
    else:
        origin, lic_name, lic_url = extract_license_origin(mc_pom(g, a, v))
        res["origin"] = origin or f"https://central.sonatype.com/artifact/{g}/{a}"
        res["license_name"], res["license_url"] = lic_name, lic_url
        res["license_type"] = normalize_license(lic_name) or "unknown"
    cache[key] = res
    return res


# --------------------------- Confluence approved-list check ---------------------------

def lic_family(s):
    s = (s or "").lower()
    for key, fam in [("apache", "Apache"), ("mit", "MIT"), ("isc", "MIT"), ("epl", "EPL"),
                     ("eclipse public", "EPL"), ("edl", "EDL"), ("eclipse distribution", "EDL"),
                     ("cddl", "CDDL"), ("lgpl", "LGPL"), ("lesser", "LGPL"),
                     ("postgresql", "BSD"), ("bsd", "BSD"), ("bouncy", "Bouncy"), ("gpl", "GPL")]:
        if key in s:
            return fam
    return s.strip() or "?"


def _compact(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _tokens(s):
    s = re.sub(r"([a-z])([0-9])", r"\1 \2", s or "")
    parts = re.split(r"[^a-zA-Z0-9]+", s.lower())
    stop = {"api", "core", "starter", "impl", "runtime", "support", "library", "libraries",
            "module", "the", "for", "and", "java", "registry", "simpleclient", "client",
            "server", "io", "test", "apache"}
    return {p for p in parts if p and p not in stop and (len(p) >= 3 or any(c.isdigit() for c in p))}


def fetch_confluence_rows(base, page_id, email, token):
    """Return list of {name,url,license} parsed from the page's storage-format tables."""
    url = f"{base.rstrip('/')}/wiki/api/v2/pages/{page_id}?body-format=storage"
    try:
        data = json.loads(http_get(url, {"User-Agent": "deps-inventory", "Accept": "application/json"},
                                   auth=(email, token)))
    except Exception as e:
        print(f"  [confluence] fetch failed: {e}", file=sys.stderr)
        return []
    html = data.get("body", {}).get("storage", {}).get("value", "")
    rows = []
    for tr in re.findall(r"<tr\b.*?</tr>", html, re.S | re.I):
        cells = re.findall(r"<t[dh]\b.*?>(.*?)</t[dh]>", tr, re.S | re.I)
        if len(cells) < 4:
            continue
        def clean(c):
            href = re.search(r'href="([^"]+)"', c)
            txt = re.sub(r"<[^>]+>", " ", c)
            txt = re.sub(r"&amp;", "&", txt)
            return re.sub(r"\s+", " ", txt).strip(), (href.group(1) if href else "")
        name, _ = clean(cells[0])
        _, url2 = clean(cells[2]) if len(cells) > 2 else ("", "")
        lic, _ = clean(cells[3]) if len(cells) > 3 else ("", "")
        if name and name.lower() not in ("name",):
            rows.append({"name": name, "url": url2, "license": lic})
    return rows


def build_page_index(rows):
    P = []
    for r in rows:
        if not (r["name"] or "").strip():
            continue
        blob = r["name"] + " " + r["url"]
        P.append({"name": r["name"], "compact": _compact(blob), "tok": _tokens(blob),
                  "lic": lic_family(r["license"])})
    return P


def match_page(dep_g, dep_a, P):
    """Return list of matching page-entry names (heuristic: name/url tokens or compact substring)."""
    dc, dtok = _compact(dep_a), _tokens(dep_a)
    gseg = _compact(dep_g.split(".")[-1])
    out = []
    for p in P:
        if len(dc) >= 6 and (dc in p["compact"] or (len(p["compact"]) >= 6 and p["compact"] in dc)):
            out.append(p)
        elif dtok and (dtok <= p["tok"] or (p["tok"] and p["tok"] <= dtok)):
            out.append(p)
        elif dtok and (dtok & p["tok"]) and len(gseg) >= 4 and gseg in p["compact"]:
            out.append(p)
    return out


# --------------------------- Output ---------------------------

def md_link(url, label="link"):
    return f"[{label}]({url})" if url else ""


def repos_fmt(repos):
    rl = sorted(repos)
    if len(rl) == 1:
        return f"`{rl[0]}`"
    return f"`{rl[0]}` and {len(rl) - 1} other" + ("s" if len(rl) - 1 != 1 else "")


def write_reports(out_dir, repos, by_repo, skipped, sp_ref, uniq, approved_enabled):
    os.makedirs(out_dir, exist_ok=True)
    total = sum(len(v) for v in by_repo.values())

    with open(os.path.join(out_dir, "dependencies-by-repository.md"), "w") as f:
        f.write("# Symphony microservices — third-party dependency inventory\n\n")
        f.write("Explicit third-party (open-source) dependencies declared in each repo's `pom.xml` "
                "(`com.avispl.*` excluded). Versions shown when declared, else a reference to the "
                "managing BOM/parent.\n\n")
        f.write(f"**{len(by_repo)} repos with poms · {total} dependency rows.**\n\n")
        if skipped:
            f.write("## Skipped repos (no Maven pom)\n\n")
            for r, why in skipped.items():
                f.write(f"- `{r}` — {why}\n")
            f.write("\n")
        f.write("---\n\n")
        for repo in repos:
            if repo not in by_repo:
                continue
            rows = by_repo[repo]
            ref = rows[0]["ref"] if rows else "?"
            f.write(f"## {repo}\n\n_ref: `{ref}` · {len(rows)} third-party deps_\n\n")
            if not rows:
                f.write("_No third-party dependencies declared._\n\n")
                continue
            f.write("| Dependency | Version / source | Origin | License | Type | Scope |\n|---|---|---|---|---|---|\n")
            for r in rows:
                ga = f"`{r['groupId']}:{r['artifactId']}`" + (f"<br>_(module: {r['module']})_" if r["module"] else "")
                f.write(f"| {ga} | {r['version']} | {md_link(r['origin'])} | {md_link(r['license_url'])} "
                        f"| {r['license_type']} | {r['scope']} |\n")
            f.write("\n")

    with open(os.path.join(out_dir, "dependencies.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["repository", "ref", "module", "groupId", "artifactId", "version_or_source",
                    "scope", "origin", "license_url", "license_type", "approved"])
        for repo in repos:
            for r in by_repo.get(repo, []):
                ver = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", r["version"])
                w.writerow([r["repository"], r["ref"], r["module"], r["groupId"], r["artifactId"], ver,
                            r["scope"], r["origin"], r["license_url"], r["license_type"], r.get("approved", "")])

    with open(os.path.join(out_dir, "unique-dependencies.md"), "w") as f:
        f.write("# Unique third-party dependencies (deduped across all repos)\n\n")
        f.write(f"**{len(uniq)} distinct artifacts.**")
        if approved_enabled:
            red = sorted(ga for ga, u in uniq.items() if u["status"] == "RED")
            f.write(f" Cross-checked against the approved 3rd-party Confluence page.\n\n")
            f.write(f"## 🔴 {len(red)} dependencies NOT on the approved list\n\n")
            for ga in red:
                f.write(f"- 🔴 **{ga}** — {uniq[ga]['license_type']} · {repos_fmt(uniq[ga]['repos'])}\n")
            f.write("\n> RED = no automatic name/URL match on the page — review manually "
                    "(a library may be listed under a different product name).\n\n---\n\n")
        else:
            f.write(" (Confluence cross-check disabled.)\n\n---\n\n")
        f.write("| Approved? | Dependency | License | Type | Origin | Repositories |\n|---|---|---|---|---|---|\n")
        for ga in sorted(uniq):
            u = uniq[ga]
            status = {"RED": "🔴 **NOT LISTED**", "YELLOW": "🟡 via product entry",
                      "OK": "✅", "NA": "—"}.get(u["status"], "—")
            f.write(f"| {status} | `{ga}` | {md_link(u['license_url'])} | {u['license_type']} "
                    f"| {md_link(u['origin'])} | {repos_fmt(u['repos'])} |\n")


def write_allure(adir, by_repo, uniq, approved_enabled, meta):
    """One Allure test result per (repo, dependency). Not-on-approved-list = failed."""
    os.makedirs(adir, exist_ok=True)
    now = int(time.time() * 1000)
    for repo, rows in by_repo.items():
        for r in rows:
            ga = f"{r['groupId']}:{r['artifactId']}"
            u = uniq.get(ga, {})
            status_key = u.get("status", "NA")
            if not approved_enabled:
                status, msg = "passed", "Inventory only (approved-list check disabled)"
            elif status_key == "RED":
                status, msg = "failed", "Not found on the approved 3rd-party Confluence list (review)"
            elif status_key == "YELLOW":
                status, msg = "passed", "Approved via a product-level entry on the page"
            else:
                status, msg = "passed", "On the approved 3rd-party list"
            res = {
                "uuid": str(uuid.uuid4()),
                "historyId": f"{repo}:{ga}",
                "name": f"{ga}",
                "fullName": f"{repo} / {ga}",
                "status": status,
                "statusDetails": {"message": msg},
                "stage": "finished", "start": now, "stop": now,
                "labels": [
                    {"name": "parentSuite", "value": "Symphony third-party dependencies"},
                    {"name": "suite", "value": repo},
                    {"name": "subSuite", "value": r["scope"]},
                    {"name": "feature", "value": u.get("license_type", r["license_type"])},
                    {"name": "story", "value": r["groupId"]},
                ],
                "links": [x for x in (
                    {"type": "link", "name": "origin", "url": r["origin"]} if r["origin"] else None,
                    {"type": "link", "name": "license", "url": r["license_url"]} if r["license_url"] else None,
                ) if x],
                "parameters": [
                    {"name": "version / source", "value": re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", r["version"])},
                    {"name": "license type", "value": r["license_type"]},
                    {"name": "ref", "value": r["ref"]},
                ],
            }
            with open(os.path.join(adir, f"{res['uuid']}-result.json"), "w") as f:
                json.dump(res, f)

    # Categories + environment for richer Allure tabs.
    with open(os.path.join(adir, "categories.json"), "w") as f:
        json.dump([{"name": "Not on approved 3rd-party list",
                    "matchedStatuses": ["failed"],
                    "messageRegex": ".*approved.*"}], f)
    with open(os.path.join(adir, "environment.properties"), "w") as f:
        for k, v in meta.items():
            f.write(f"{k}={v}\n")


# --------------------------- main ---------------------------

def attribute_source(ga, g, sp_managed, sbd_managed, own_managed, sp_ref, org, sbd_ver):
    sp_link = f"https://github.com/{org}/symphony-shared-parent/blob/{sp_ref}/pom.xml"
    sbd_link = (f"https://repo1.maven.org/maven2/org/springframework/boot/spring-boot-dependencies/"
                f"{sbd_ver}/spring-boot-dependencies-{sbd_ver}.pom")
    if ga in own_managed:
        return "managed by this pom's dependencyManagement / imported BOM"
    if ga in sp_managed:
        return f"managed by [symphony-shared-parent]({sp_link})"
    if ga in sbd_managed:
        return f"managed by [spring-boot-dependencies {sbd_ver}]({sbd_link})"
    if g.startswith("com.google"):
        return "managed by google-cloud libraries-bom (per-repo import)"
    if g.startswith("io.netty"):
        return "managed by netty-bom"
    if g.startswith("software.amazon"):
        return "managed by AWS SDK BOM"
    return "managed externally (BOM / parent — not pinned in this pom)"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", required=True, help="JSON array of repo names")
    ap.add_argument("--org", default="AVISPL")
    ap.add_argument("--out-dir", default="out")
    ap.add_argument("--allure-results", default=None, help="dir for Allure result JSON")
    ap.add_argument("--prefer-ref", default="release/6.27")
    ap.add_argument("--confluence-page", default=None, help="Confluence page id for approved-list check")
    args = ap.parse_args()

    if not _gh_token():
        print("ERROR: set GH_TOKEN or GITHUB_TOKEN with read access to the org.", file=sys.stderr)
        sys.exit(2)

    repos = json.load(open(args.list))

    print("Fetching reference BOMs...", file=sys.stderr)
    sp_ref = resolve_ref(args.org, "symphony-shared-parent", args.prefer_ref)
    sp_pom = fetch_pom(args.org, "symphony-shared-parent", sp_ref)
    sp_managed = managed_ga_set(sp_pom)
    sbd_ver = SPRING_BOOT_FALLBACK
    if sp_pom:
        m = re.search(r"<spring-boot\.version>([^<]+)</spring-boot\.version>", sp_pom)
        if m:
            sbd_ver = m.group(1)
    sbd_managed = managed_ga_set(mc_pom("org.springframework.boot", "spring-boot-dependencies", sbd_ver))
    print(f"  shared-parent@{sp_ref}: {len(sp_managed)} GAs; spring-boot-deps {sbd_ver}: {len(sbd_managed)} GAs",
          file=sys.stderr)

    # Optional Confluence approved-list index.
    page_index, approved_enabled = [], False
    cbase = os.environ.get("CONFLUENCE_BASE_URL")
    cemail = os.environ.get("CONFLUENCE_EMAIL")
    ctok = os.environ.get("CONFLUENCE_API_TOKEN")
    if args.confluence_page and cbase and cemail and ctok:
        rows = fetch_confluence_rows(cbase, args.confluence_page, cemail, ctok)
        page_index = build_page_index(rows)
        approved_enabled = len(page_index) > 0
        print(f"  confluence page {args.confluence_page}: {len(page_index)} approved entries", file=sys.stderr)
    else:
        print("  confluence check disabled (page id or CONFLUENCE_* secrets missing)", file=sys.stderr)

    cache = {}
    by_repo, skipped = {}, {}
    for repo in repos:
        if repo in NON_JAVA:
            skipped[repo] = NON_JAVA[repo]
            continue
        ref = resolve_ref(args.org, repo, args.prefer_ref)
        root_pom = fetch_pom(args.org, repo, ref)
        if root_pom is None:
            skipped[repo] = f"no pom.xml at ref {ref}"
            continue
        poms = [("", root_pom)]
        try:
            rroot = ET.fromstring(root_pom)
            mods = fel(rroot, "modules")
            if mods is not None:
                for m in mods:
                    if m.text:
                        mp = fetch_pom(args.org, repo, ref, f"{m.text.strip()}/pom.xml")
                        if mp:
                            poms.append((m.text.strip(), mp))
        except ET.ParseError:
            pass

        rows, seen = [], set()
        for module, pom_text in poms:
            try:
                root = ET.fromstring(pom_text)
            except ET.ParseError:
                continue
            props = parse_properties(root)
            own_managed = managed_ga_set(pom_text)
            for g, a, v_raw, scope in iter_direct_dependencies(root):
                if g.startswith("com.avispl"):
                    continue
                ga = f"{g}:{a}"
                if (module, ga) in seen:
                    continue
                seen.add((module, ga))
                v_res = resolve_prop(v_raw, props) if v_raw else None
                if v_res:
                    version_field = v_res + (f"  (from {v_raw})" if v_raw != v_res else "")
                else:
                    version_field = attribute_source(ga, g, sp_managed, sbd_managed, own_managed,
                                                      sp_ref, args.org, sbd_ver)
                lic = enrich(g, a, cache)
                rows.append({"repository": repo, "ref": ref, "module": module, "groupId": g,
                             "artifactId": a, "version": version_field, "scope": scope or "compile",
                             "origin": lic["origin"] or "", "license_url": lic["license_url"] or "",
                             "license_type": lic["license_type"] or "unknown"})
        rows.sort(key=lambda r: (r["module"], r["groupId"], r["artifactId"]))
        by_repo[repo] = rows
        print(f"  {repo}@{ref}: {len(rows)} third-party deps", file=sys.stderr)

    # Build unique index + approval status.
    uniq = {}
    for repo in repos:
        for r in by_repo.get(repo, []):
            ga = f"{r['groupId']}:{r['artifactId']}"
            u = uniq.setdefault(ga, {"origin": r["origin"], "license_url": r["license_url"],
                                     "license_type": r["license_type"], "repos": set(), "status": "NA"})
            u["repos"].add(repo)
    if approved_enabled:
        for ga, u in uniq.items():
            g, a = ga.split(":", 1)
            cands = match_page(g, a, page_index)
            if not cands:
                u["status"] = "RED"
            elif any(c["lic"] == lic_family(u["license_type"]) for c in cands):
                u["status"] = "OK"
            else:
                u["status"] = "YELLOW"  # matched by name but license family differs
    # propagate approval onto per-repo rows for CSV/Allure
    for rows in by_repo.values():
        for r in rows:
            r["approved"] = uniq.get(f"{r['groupId']}:{r['artifactId']}", {}).get("status", "NA")

    write_reports(args.out_dir, repos, by_repo, skipped, sp_ref, uniq, approved_enabled)
    if args.allure_results:
        red = sum(1 for u in uniq.values() if u["status"] == "RED")
        meta = {"org": args.org, "prefer_ref": args.prefer_ref, "repos_with_poms": len(by_repo),
                "total_dependency_rows": sum(len(v) for v in by_repo.values()),
                "distinct_artifacts": len(uniq), "approved_list_check": approved_enabled,
                "not_on_approved_list": red}
        write_allure(args.allure_results, by_repo, uniq, approved_enabled, meta)
    print(f"\nDone. out-dir={args.out_dir} allure={args.allure_results}", file=sys.stderr)


if __name__ == "__main__":
    main()
