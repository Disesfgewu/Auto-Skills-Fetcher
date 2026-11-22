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
from .pack_manager import (
    PackManager,
    build_semantic_diff,
    bundle_cache_dir,
    load_yaml_config,
    resolve_agent_targets,
    resolve_pack_skills,
    save_skill_bundle,
)
from .parser import parse_skill_md
from .registry import SkillRegistry
from .scorer import evaluate_skill
from .security import scan_skill_security

DEFAULT_DB = Path(".skillpack/registry.sqlite")
DEFAULT_PACKS_CFG = Path("config/packs.yaml")
DEFAULT_SOURCES_CFG = Path("config/sources.yaml")


def _extract_script_texts(extra_files: dict[str, bytes]) -> dict[str, str]:
    scripts: dict[str, str] = {}
    for rel, raw in extra_files.items():
        if len(raw) > 65_536:
            continue
        if rel.startswith("scripts/") or rel.endswith((".sh", ".py", ".js", ".ts", ".ps1")):
            try:
                scripts[rel] = raw.decode("utf-8")
            except UnicodeDecodeError:
                continue
    return scripts


def _sync_preset_skills(
    packs_cfg: dict,
    sources_cfg: dict,
    reg: SkillRegistry,
    db_path: str | Path,
    *,
    pack_name: str | None = None,
    fetch: bool = False,
    crawler: GitHubCrawler | None = None,
) -> tuple[int, int]:
    """Seed or fetch preset pack skills into the local registry and bundle cache."""
    packs = packs_cfg.get("packs", {})
    if pack_name and pack_name != "all":
        resolved = resolve_pack_skills(packs_cfg, pack_name)
        items_with_ctx = [
            (
                item["source_pack"],
                packs[item["source_pack"]].get("domain", "base"),
                packs[item["source_pack"]].get("description", item["source_pack"]),
                item,
            )
            for item in resolved
        ]
    else:
        items_with_ctx = []
        for pname, pack in packs.items():
            dom = pack.get("domain", "base")
            pdesc = pack.get("description", pname)
            for item in pack.get("skills", []):
                items_with_ctx.append((pname, dom, pdesc, item))

    active_crawler = crawler or (GitHubCrawler() if fetch else None)
    synced = 0
    failed = 0
    seen_ids: set[str] = set()

    for pname, domain, pdesc, item in items_with_ctx:
        skill_id = f"{item['repo']}:{item['path']}"
        if skill_id in seen_ids:
            continue
        seen_ids.add(skill_id)

        if not fetch:
            stub_md = (
                f"---\nname: {item['name']}\n"
                f"description: Preset skill '{item['name']}' from {item['repo']} for {pname}.\n"
                f"---\n\n# {item['name']}\n\nWhen to use: {pdesc}.\n"
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
            synced += 1
            continue

        assert active_crawler is not None
        try:
            raw_md, sha, extra_files = active_crawler.fetch_skill_bundle(item["repo"], item["path"])
        except (urllib.error.HTTPError, urllib.error.URLError) as exc:
            print(f"  [FAIL] {item['name']} ({item['repo']}/{item['path']}): {exc}", file=sys.stderr)
            failed += 1
            continue

        if not raw_md:
            print(f"  [FAIL] {item['name']} ({item['repo']}/{item['path']}): empty response", file=sys.stderr)
            failed += 1
            continue

        has_scripts = any(p.startswith("scripts/") for p in extra_files)
        has_refs = any(
            p.startswith("references/") or p.lower().endswith("reference.md") for p in extra_files
        )
        sec = scan_skill_security(raw_md, extra_scripts=_extract_script_texts(extra_files))
        try:
            doc = parse_skill_md(raw_md, has_scripts=has_scripts, has_references=has_refs)
        except ValueError as exc:
            print(f"  [FAIL] {item['name']} parse error: {exc}", file=sys.stderr)
            failed += 1
            continue

        meta = active_crawler.fetch_repo_metadata(item["repo"])
        rec = SkillRecord(
            skill_id=skill_id,
            name=item["name"],
            description=doc.description,
            domain=domain,
            repo=item["repo"],
            path=item["path"],
            url=item.get("url", f"https://github.com/{item['repo']}"),
            commit_sha=sha or "upstream",
            content_hash=doc.content_hash,
            raw_content=raw_md,
            stars=int(meta.get("stars", 0)),
            curated_mentions=1,
            days_since_update=int(meta.get("days_since_update", 30)),
            license_spdx=str(meta.get("license_spdx", "")),
            has_scripts=has_scripts,
            has_references=has_refs,
            estimated_tokens=doc.estimated_tokens,
            security_passed=sec.passed,
            slot=item.get("slot"),
            is_champion=True,
        )
        rec.score = evaluate_skill(rec, doc, config=sources_cfg)
        reg.upsert_skill(rec)
        save_skill_bundle(bundle_cache_dir(db_path, skill_id), extra_files)
        status_tag = "OK" if sec.passed else "BLOCKED"
        print(
            f"  [{status_tag}] {item['name']} ({item['repo']}) — "
            f"{len(raw_md)}B SKILL.md + {len(extra_files)} assets, score={rec.score.total}"
        )
        synced += 1

    return synced, failed


def _cmd_init(args: argparse.Namespace) -> int:
    packs_cfg = load_yaml_config(args.packs_config)
    sources_cfg = load_yaml_config(args.sources_config)
    reg = SkillRegistry(args.db)
    try:
        synced, failed = _sync_preset_skills(
            packs_cfg, sources_cfg, reg, args.db, fetch=bool(args.fetch)
        )
        mode = "fetched upstream" if args.fetch else "preset"
        print(f"Initialized registry at {args.db} with {synced} {mode} champion skills.")
        return 1 if failed and synced == 0 else 0
    finally:
        reg.close()


def _cmd_sync(args: argparse.Namespace) -> int:
    packs_cfg = load_yaml_config(args.packs_config)
    sources_cfg = load_yaml_config(args.sources_config)
    reg = SkillRegistry(args.db)
    try:
        synced, failed = _sync_preset_skills(
            packs_cfg, sources_cfg, reg, args.db, pack_name=args.pack, fetch=True
        )
        print(f"Synced {synced} upstream skills into {args.db} (failed: {failed}).")
        return 1 if failed and synced == 0 else 0
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
                meta = crawler.fetch_repo_metadata(item["repo"])
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
                    stars=int(meta.get("stars", 0)),
                    days_since_update=int(meta.get("days_since_update", 30)),
                    license_spdx=str(meta.get("license_spdx", "")),
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
    if not args.target and not args.agent:
        print("Error: specify either --target <dir> or --agent {agy,claude,codex,all}.", file=sys.stderr)
        return 2

    packs_cfg = load_yaml_config(args.packs_config)
    sources_cfg = load_yaml_config(args.sources_config)
    reg = SkillRegistry(args.db)
    try:
        resolved = resolve_pack_skills(packs_cfg, args.pack)
        indexed = {s.skill_id: s for s in reg.list_skills()}

        needs_fetch = any(
            (f"{item['repo']}:{item['path']}" not in indexed)
            or (indexed[f"{item['repo']}:{item['path']}"].commit_sha == "preset-seed")
            for item in resolved
        )
        if args.fetch and needs_fetch:
            print(f"Fetching upstream skills for pack '{args.pack}'...")
            _sync_preset_skills(packs_cfg, sources_cfg, reg, args.db, pack_name=args.pack, fetch=True)
            indexed = {s.skill_id: s for s in reg.list_skills()}

        payloads = []
        stub_count = 0
        for item in resolved:
            sid = f"{item['repo']}:{item['path']}"
            rec = indexed.get(sid)
            if rec:
                if rec.commit_sha == "preset-seed":
                    stub_count += 1
                payloads.append(
                    {
                        "name": rec.name,
                        "description": rec.description,
                        "slot": item.get("slot"),
                        "repo": rec.repo,
                        "path": rec.path,
                        "commit_sha": rec.commit_sha,
                        "raw_content": rec.raw_content,
                        "bundle_dir": str(bundle_cache_dir(args.db, sid)),
                    }
                )

        targets: list[tuple[str, Path]] = []
        if args.agent:
            targets.extend(
                resolve_agent_targets(
                    args.agent, scope=args.scope, project_root=args.project_root
                )
            )
        if args.target:
            targets.append(("custom", Path(args.target)))

        policy = packs_cfg["packs"][args.pack].get("update_policy", "track-with-diff")
        for label, target_path in targets:
            mgr = PackManager(target_path)
            warnings = mgr.assemble(
                args.pack,
                payloads,
                update_policy=policy,
                prune_previous=bool(args.prune),
            )
            prefix = f"[{label}] " if label != "custom" else ""
            print(f"{prefix}Assembled pack '{args.pack}' ({len(payloads)} skills) -> {target_path}")
            for w in warnings:
                print(
                    f"  [COLLISION WARNING] '{w.skill_a}' <-> '{w.skill_b}' (similarity={w.similarity})"
                )

        if stub_count > 0:
            print(
                f"  [NOTE] {stub_count} skill(s) are offline stubs. "
                f"Run 'skillpack sync --pack {args.pack}' or pass '--fetch' to download full upstream skills."
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
    p_init.add_argument(
        "--fetch",
        action="store_true",
        help="Fetch full upstream SKILL.md and bundled assets from GitHub instead of offline stubs.",
    )
    p_init.set_defaults(func=_cmd_init)

    p_sync = sub.add_parser("sync", help="Download full upstream SKILL.md and assets for preset packs.")
    p_sync.add_argument("--pack", default="all", help="Pack name from packs.yaml or 'all'.")
    p_sync.set_defaults(func=_cmd_sync)

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

    p_asm = sub.add_parser("assemble", help="Assemble a skill pack into target or agent skill directories.")
    p_asm.add_argument("--pack", required=True, help="Pack name from packs.yaml.")
    p_asm.add_argument("--target", help="Custom output directory for assembled skills.")
    p_asm.add_argument(
        "--agent",
        choices=["agy", "claude", "codex", "all"],
        help="Install directly into native skill directory for agy, claude, codex, or all.",
    )
    p_asm.add_argument(
        "--scope",
        choices=["project", "global"],
        default="project",
        help="Target scope when using --agent (default: project).",
    )
    p_asm.add_argument(
        "--project-root",
        default=".",
        help="Project root directory when using --agent with --scope project.",
    )
    p_asm.add_argument(
        "--fetch",
        action="store_true",
        help="Automatically fetch real upstream skills from GitHub if missing or still stubbed.",
    )
    p_asm.add_argument(
        "--prune",
        action="store_true",
        help="Remove previously skillpack-managed skills not in the new pack (preserves user skills).",
    )
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
