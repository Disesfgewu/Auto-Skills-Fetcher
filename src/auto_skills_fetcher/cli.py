"""Command-line interface for auto-skills-fetcher (`skillpack`)."""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
from pathlib import Path

from .crawler import GitHubCrawler, build_partitioned_queries
from .matcher import recommend_replacements
from .models import SkillRecord
from .pack_manager import PackManager, build_semantic_diff, load_yaml_config, resolve_pack_skills
from .parser import parse_skill_md
from .registry import SkillRegistry
from .scorer import evaluate_skill
from .security import scan_skill_security

DEFAULT_DB = Path(".skillpack/registry.sqlite")
DEFAULT_PACKS_CFG = Path("config/packs.yaml")
DEFAULT_SOURCES_CFG = Path("config/sources.yaml")


def _cmd_init(args: argparse.Namespace) -> int:
    packs_cfg = load_yaml_config(args.packs_config)
    sources_cfg = load_yaml_config(args.sources_config)
    reg = SkillRegistry(args.db)
    try:
        seeded = 0
        for pack_name, pack in packs_cfg.get("packs", {}).items():
            domain = pack.get("domain", "base")
            for item in pack.get("skills", []):
                skill_id = f"{item['repo']}:{item['path']}"
                stub_md = (
                    f"---\nname: {item['name']}\n"
                    f"description: Preset skill '{item['name']}' from {item['repo']} for {pack_name}.\n"
                    f"---\n\n# {item['name']}\n\nWhen to use: {pack.get('description', pack_name)}.\n"
                    f"Workflow: Step 1 verify inputs and follow upstream SKILL.md.\n"
                )
                doc = parse_skill_md(stub_md)
                rec = SkillRecord(
                    skill_id=skill_id,
                    name=item["name"],
                    description=doc.description,
                    domain=domain,
                    repo=item["repo"],
                    path=item["path"],
                    url=item.get("url", f"https://github.com/{item['repo']}"),
                    commit_sha="preset-seed",
                    content_hash=doc.content_hash,
                    raw_content=stub_md,
                    stars=500,
                    curated_mentions=1,
                    days_since_update=30,
                    license_spdx="Apache-2.0",
                    estimated_tokens=doc.estimated_tokens,
                    security_passed=True,
                    slot=item.get("slot"),
                    is_champion=True,
                )
                rec.score = evaluate_skill(rec, doc, config=sources_cfg)
                reg.upsert_skill(rec)
                seeded += 1
        print(f"Initialized registry at {args.db} with {seeded} preset champion skills.")
        return 0
    finally:
        reg.close()


def _cmd_status(args: argparse.Namespace) -> int:
    packs_cfg = load_yaml_config(args.packs_config)
    reg = SkillRegistry(args.db)
    try:
        st = reg.stats()
        print("Auto-Skills-Fetcher Status")
        print(f"  Database: {args.db}")
        print(f"  Indexed skills: {st['total_skills']} (champions: {st['champions']}, blocked: {st['blocked']})")
        print(f"  Pending recommendations: {st['pending_recommendations']}")
        print("  Configured packs:")
        for name, cfg in packs_cfg.get("packs", {}).items():
            resolved = resolve_pack_skills(packs_cfg, name)
            print(
                f"    - {name} (domain={cfg.get('domain')}, policy={cfg.get('update_policy')}, "
                f"skills={len(resolved)})"
            )
        return 0
    finally:
        reg.close()


def _cmd_crawl(args: argparse.Namespace) -> int:
    sources_cfg = load_yaml_config(args.sources_config)
    domain_queries = sources_cfg.get("domain_queries", {})
    domains = list(domain_queries.keys()) if args.domain == "all" else [args.domain]
    crawler = GitHubCrawler()
    reg = SkillRegistry(args.db)
    try:
        for dom in domains:
            if dom not in domain_queries:
                print(f"Unknown domain '{dom}'.", file=sys.stderr)
                return 2
            if args.dry_run:
                qs = build_partitioned_queries(domain_queries[dom])
                print(f"[DRY-RUN] Domain '{dom}' generated {len(qs)} partitioned queries:")
                for q in qs:
                    print(f"  - {q}")
                continue
            items = crawler.search_domain(dom, domain_queries[dom], max_items=args.limit)
            ingested = 0
            for item in items:
                try:
                    raw, sha, _ = crawler.fetch_skill_file(item["repo"], item["path"])
                except urllib.error.HTTPError:
                    continue
                if not raw:
                    continue
                sec = scan_skill_security(raw)
                try:
                    doc = parse_skill_md(raw)
                except ValueError:
                    continue
                if reg.has_content_hash(doc.content_hash):
                    continue
                rec = SkillRecord(
                    skill_id=f"{item['repo']}:{item['path']}",
                    name=doc.name,
                    description=doc.description,
                    domain=dom,
                    repo=item["repo"],
                    path=item["path"],
                    url=item["url"],
                    commit_sha=sha or item.get("sha", ""),
                    content_hash=doc.content_hash,
                    raw_content=raw,
                    estimated_tokens=doc.estimated_tokens,
                    security_passed=sec.passed,
                    is_champion=False,
                )
                rec.score = evaluate_skill(rec, doc, config=sources_cfg)
                reg.upsert_skill(rec)
                ingested += 1
            print(f"Domain '{dom}': ingested {ingested} unique skills.")
        return 0
    finally:
        reg.close()


def _cmd_recommend(args: argparse.Namespace) -> int:
    sources_cfg = load_yaml_config(args.sources_config)
    reg = SkillRegistry(args.db)
    try:
        dom = None if args.domain == "all" else args.domain
        champions = reg.list_skills(domain=dom, champions_only=True)
        candidates = reg.list_skills(domain=dom, candidates_only=True)
        recs = recommend_replacements(champions, candidates, config=sources_cfg)
        if recs:
            reg.save_recommendations(recs)
        if args.json:
            print(json.dumps([r.__dict__ for r in recs], indent=2, ensure_ascii=False))
        elif not recs:
            print("No challengers exceeded the replacement hysteresis margin.")
        else:
            for r in recs:
                print(f"[{r.kind.upper()}] ({r.domain}) {r.rationale}")
        return 0
    finally:
        reg.close()


def _cmd_assemble(args: argparse.Namespace) -> int:
    packs_cfg = load_yaml_config(args.packs_config)
    reg = SkillRegistry(args.db)
    try:
        resolved = resolve_pack_skills(packs_cfg, args.pack)
        indexed = {s.skill_id: s for s in reg.list_skills()}
        payloads = []
        for item in resolved:
            sid = f"{item['repo']}:{item['path']}"
            rec = indexed.get(sid)
            if rec:
                payloads.append(
                    {
                        "name": rec.name,
                        "description": rec.description,
                        "slot": item.get("slot"),
                        "repo": rec.repo,
                        "path": rec.path,
                        "commit_sha": rec.commit_sha,
                        "raw_content": rec.raw_content,
                    }
                )
        mgr = PackManager(args.target)
        policy = packs_cfg["packs"][args.pack].get("update_policy", "track-with-diff")
        warnings = mgr.assemble(args.pack, payloads, update_policy=policy)
        print(f"Assembled pack '{args.pack}' ({len(payloads)} skills) -> {args.target}")
        for w in warnings:
            print(
                f"  [COLLISION WARNING] '{w.skill_a}' <-> '{w.skill_b}' (similarity={w.similarity})"
            )
        return 0
    finally:
        reg.close()


def _cmd_update(args: argparse.Namespace) -> int:
    mgr = PackManager(args.target)
    installed_file = Path(args.target) / args.skill / "SKILL.md"
    incoming_file = Path(args.incoming)
    old_content = installed_file.read_text(encoding="utf-8")
    new_content = incoming_file.read_text(encoding="utf-8")

    sec = scan_skill_security(new_content)
    if not sec.passed:
        print(f"[BLOCKED] Incoming skill failed security scan: {[f.rule_id for f in sec.findings]}")
        return 1

    diff = build_semantic_diff(args.skill, old_content, new_content)
    print(f"Diff summary for '{args.skill}': {diff.summary}")
    if args.show_diff:
        print(diff.unified_diff)

    decision = args.decision
    if decision == "interactive":
        choice = input("Choose action [replace / keep / side-by-side]: ").strip().lower()
        decision = choice if choice in {"replace", "keep", "side-by-side"} else "keep"

    msg = mgr.apply_update(args.skill, new_content, new_commit_sha=args.sha, decision=decision)
    print(msg)
    return 0


def _cmd_rollback(args: argparse.Namespace) -> int:
    mgr = PackManager(args.target)
    print(mgr.rollback(args.skill))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="skillpack",
        description="Auto-Skills-Fetcher: crawl, score, recommend, and assemble AI Agent Skill packs.",
    )
    parser.add_argument("--db", default=str(DEFAULT_DB), help="Path to SQLite registry.")
    parser.add_argument("--packs-config", default=str(DEFAULT_PACKS_CFG), help="Path to packs.yaml.")
    parser.add_argument("--sources-config", default=str(DEFAULT_SOURCES_CFG), help="Path to sources.yaml.")

    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="Initialize registry with preset champion packs.")
    p_init.set_defaults(func=_cmd_init)

    p_status = sub.add_parser("status", help="Show registry and configured pack status.")
    p_status.set_defaults(func=_cmd_status)

    p_crawl = sub.add_parser("crawl", help="Crawl GitHub for SKILL.md candidates by domain.")
    p_crawl.add_argument("--domain", default="all", help="Domain name or 'all'.")
    p_crawl.add_argument("--limit", type=int, default=30, help="Max items per domain.")
    p_crawl.add_argument("--dry-run", action="store_true", help="Print partitioned queries without network calls.")
    p_crawl.set_defaults(func=_cmd_crawl)

    p_rec = sub.add_parser("recommend", help="Run Champion-Challenger replacement scoring.")
    p_rec.add_argument("--domain", default="all", help="Domain name or 'all'.")
    p_rec.add_argument("--json", action="store_true", help="Output JSON.")
    p_rec.set_defaults(func=_cmd_recommend)

    p_asm = sub.add_parser("assemble", help="Assemble a skill pack into a target directory.")
    p_asm.add_argument("--pack", required=True, help="Pack name from packs.yaml.")
    p_asm.add_argument("--target", required=True, help="Output directory for assembled skills.")
    p_asm.set_defaults(func=_cmd_assemble)

    p_upd = sub.add_parser("update", help="Review diff and selectively replace/keep/side-by-side a skill.")
    p_upd.add_argument("--target", required=True, help="Assembled pack directory.")
    p_upd.add_argument("--skill", required=True, help="Skill name to update.")
    p_upd.add_argument("--incoming", required=True, help="Path to incoming SKILL.md.")
    p_upd.add_argument("--sha", default="local-update", help="Commit SHA of incoming skill.")
    p_upd.add_argument("--decision", choices=["replace", "keep", "side-by-side", "interactive"], default="interactive")
    p_upd.add_argument("--show-diff", action="store_true", help="Print unified diff.")
    p_upd.set_defaults(func=_cmd_update)

    p_rb = sub.add_parser("rollback", help="Roll back a replaced skill to its previous backup.")
    p_rb.add_argument("--target", required=True, help="Assembled pack directory.")
    p_rb.add_argument("--skill", required=True, help="Skill name to roll back.")
    p_rb.set_defaults(func=_cmd_rollback)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
