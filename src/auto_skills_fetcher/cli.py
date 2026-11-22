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
    apply_pack_expansion,
    build_semantic_diff,
    bundle_cache_dir,
    load_yaml_config,
    plan_pack_expansion,
    resolve_agent_targets,
    resolve_pack_skills,
    save_skill_bundle,
    save_yaml_config,
)
from .parser import compute_content_hash, parse_skill_md
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
    packs_config_path: str | Path | None = None,
) -> tuple[int, int]:
    """Seed or fetch preset pack skills into the local registry and bundle cache."""
    packs = packs_cfg.get("packs", {})
    repo_root = (
        Path(packs_config_path).resolve().parents[1]
        if packs_config_path
        else Path.cwd()
    )
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

        # Support first-party local skills bundled in this repository (works both offline & online)
        local_rel = item.get("local_path")
        if local_rel:
            local_file = Path(local_rel)
            if not local_file.is_absolute():
                local_file = repo_root / local_rel
            if local_file.is_file():
                raw_md = local_file.read_text(encoding="utf-8")
                extra_files: dict[str, bytes] = {}
                for sibling in local_file.parent.rglob("*"):
                    if sibling.is_file() and sibling != local_file:
                        rel = sibling.relative_to(local_file.parent)
                        if "__pycache__" in rel.parts or sibling.suffix in {".pyc", ".pyo"}:
                            continue
                        extra_files[str(rel)] = sibling.read_bytes()
                has_scripts = any(p.startswith("scripts/") for p in extra_files)
                has_refs = any(
                    p.startswith("references/") or p.lower().endswith("reference.md")
                    for p in extra_files
                )
                sec = scan_skill_security(
                    raw_md,
                    extra_scripts=_extract_script_texts(extra_files),
                    raw_assets=extra_files,
                )
                doc = parse_skill_md(raw_md, has_scripts=has_scripts, has_references=has_refs)
                rec = SkillRecord(
                    skill_id=skill_id,
                    name=item["name"],
                    description=doc.description,
                    domain=domain,
                    repo=item["repo"],
                    path=item["path"],
                    url=item.get("url", f"https://github.com/{item['repo']}"),
                    commit_sha="local-builtin",
                    content_hash=doc.content_hash,
                    raw_content=raw_md,
                    stars=500,
                    curated_mentions=2,
                    days_since_update=0,
                    license_spdx="Apache-2.0",
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
                if fetch:
                    print(
                        f"  [LOCAL] {item['name']} ({local_rel}) — "
                        f"{len(raw_md)}B SKILL.md + {len(extra_files)} assets, score={rec.score.total}"
                    )
                synced += 1
                continue

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
        sec = scan_skill_security(
            raw_md,
            extra_scripts=_extract_script_texts(extra_files),
            raw_assets=extra_files,
        )
        try:
            doc = parse_skill_md(raw_md, has_scripts=has_scripts, has_references=has_refs)
        except ValueError as exc:
            print(f"  [FAIL] {item['name']} parse error: {exc}", file=sys.stderr)
            failed += 1
            continue

        meta = active_crawler.fetch_repo_metadata(item["repo"])
        stars = int(meta.get("stars", 0))
        growth_90d = reg.record_repo_snapshot(
            item["repo"], stars, created_days_ago=meta.get("created_days_ago")
        )
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
            stars=stars,
            stars_90d_growth=growth_90d,
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
        if not sec.passed:
            for f in sec.findings:
                print(f"    [SECURITY {f.severity.upper()} {f.rule_id}] {f.description} ({f.evidence})")
        synced += 1

    return synced, failed


def _cmd_init(args: argparse.Namespace) -> int:
    packs_cfg = load_yaml_config(args.packs_config)
    sources_cfg = load_yaml_config(args.sources_config)
    reg = SkillRegistry(args.db)
    try:
        synced, failed = _sync_preset_skills(
            packs_cfg,
            sources_cfg,
            reg,
            args.db,
            fetch=bool(args.fetch),
            packs_config_path=args.packs_config,
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
            packs_cfg,
            sources_cfg,
            reg,
            args.db,
            pack_name=args.pack,
            fetch=True,
            packs_config_path=args.packs_config,
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
                stars = int(meta.get("stars", 0))
                growth_90d = reg.record_repo_snapshot(
                    item["repo"], stars, created_days_ago=meta.get("created_days_ago")
                )
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
                    stars=stars,
                    stars_90d_growth=growth_90d,
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
                bdir = bundle_cache_dir(args.db, sid)
                extra_scripts: dict[str, str] = {}
                raw_assets: dict[str, bytes] = {}
                if bdir.is_dir():
                    for fpath in bdir.rglob("*"):
                        if not fpath.is_file():
                            continue
                        rel = str(fpath.relative_to(bdir))
                        raw_bytes = fpath.read_bytes()
                        raw_assets[rel] = raw_bytes
                    extra_scripts = _extract_script_texts(raw_assets)
                sec = scan_skill_security(
                    rec.raw_content, extra_scripts=extra_scripts, raw_assets=raw_assets
                )
                if not sec.passed:
                    print(
                        f"  [SECURITY BLOCKED] Refusing to assemble '{rec.name}' ({sid}): "
                        f"{[f.rule_id for f in sec.findings]}",
                        file=sys.stderr,
                    )
                    continue
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
                        "bundle_dir": str(bdir),
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


def _count_bundle_files(db_path: str | Path, skill_id: str) -> int:
    bdir = bundle_cache_dir(db_path, skill_id)
    if not bdir.is_dir():
        return 0
    return sum(1 for p in bdir.rglob("*") if p.is_file())


def _resolve_cli_targets(args: argparse.Namespace, *, default_all_agents: bool = False) -> list[tuple[str, Path]]:
    targets: list[tuple[str, Path]] = []
    agent = getattr(args, "agent", None)
    target = getattr(args, "target", None)
    if not agent and not target and default_all_agents:
        agent = "all"
    if agent:
        targets.extend(
            resolve_agent_targets(
                agent,
                scope=getattr(args, "scope", "project"),
                project_root=getattr(args, "project_root", "."),
            )
        )
    if target:
        targets.append(("custom", Path(target).expanduser().resolve()))
    return targets


def _cmd_show(args: argparse.Namespace) -> int:
    packs_cfg = load_yaml_config(args.packs_config)
    packs = packs_cfg.get("packs", {})
    reg = SkillRegistry(args.db)
    try:
        indexed = {s.skill_id: s for s in reg.list_skills()}
        targets = _resolve_cli_targets(args, default_all_agents=False)

        pack_reports: list[dict] = []
        show_packs = bool(args.pack) or not targets
        if show_packs:
            pack_names = [args.pack] if args.pack else list(packs.keys())
            for pname in pack_names:
                resolved = resolve_pack_skills(packs_cfg, pname)
                cfg = packs[pname]
                skills_out = []
                for item in resolved:
                    sid = f"{item['repo']}:{item['path']}"
                    rec = indexed.get(sid)
                    if rec is None:
                        status = "not-synced"
                        score = None
                        size_bytes = 0
                        assets = 0
                        sha = ""
                    else:
                        status = "stub" if rec.commit_sha == "preset-seed" else "upstream"
                        score = rec.score.total if rec.score else None
                        size_bytes = len(rec.raw_content.encode("utf-8"))
                        assets = _count_bundle_files(args.db, sid)
                        sha = rec.commit_sha[:8]
                    skills_out.append(
                        {
                            "name": item["name"],
                            "slot": item.get("slot"),
                            "source_pack": item.get("source_pack"),
                            "repo": item["repo"],
                            "path": item["path"],
                            "status": status,
                            "score": score,
                            "size_bytes": size_bytes,
                            "assets": assets,
                            "sha": sha,
                        }
                    )
                pack_reports.append(
                    {
                        "pack": pname,
                        "domain": cfg.get("domain"),
                        "description": cfg.get("description", ""),
                        "update_policy": cfg.get("update_policy"),
                        "extends": cfg.get("extends", []),
                        "skills": skills_out,
                    }
                )

        installed_reports: list[dict] = []
        for label, target_path in targets:
            mgr = PackManager(target_path)
            lock = mgr.read_lock()
            unmanaged = mgr.list_unmanaged_skills()
            installed_reports.append(
                {
                    "agent": label,
                    "path": str(target_path),
                    "pack": lock.get("pack") or "(none)",
                    "update_policy": lock.get("update_policy", ""),
                    "skills": lock.get("skills", {}),
                    "unmanaged_skills": unmanaged,
                }
            )

        if args.json:
            print(
                json.dumps(
                    {"packs": pack_reports, "installed": installed_reports},
                    indent=2,
                    ensure_ascii=False,
                )
            )
            return 0

        for pr in pack_reports:
            ext = f" (extends: {', '.join(pr['extends'])})" if pr["extends"] else ""
            print(f"Pack: {pr['pack']} [domain={pr['domain']}, policy={pr['update_policy']}]{ext}")
            if pr["description"]:
                print(f"  {pr['description']}")
            print(f"  {'SKILL':<24} {'STATUS':<10} {'SCORE':>6} {'SIZE':>8} {'ASSETS':>6}  REPO")
            for sk in pr["skills"]:
                sc_str = f"{sk['score']:.1f}" if sk["score"] is not None else "-"
                sz_str = f"{sk['size_bytes'] / 1024:.1f}KB" if sk["size_bytes"] else "-"
                print(
                    f"  {sk['name']:<24} {sk['status']:<10} {sc_str:>6} {sz_str:>8} {sk['assets']:>6}  {sk['repo']}"
                )
            print()

        for ir in installed_reports:
            print(f"Installed [{ir['agent']}] -> {ir['path']}")
            print(f"  Active pack: {ir['pack']} (policy={ir['update_policy'] or '-'})")
            if not ir["skills"]:
                print("  No skillpack-managed skills installed here.")
            else:
                print(f"  {'SKILL':<24} {'STATE':<12} {'SHA':<10} {'ASSETS':>6}  REPO")
                for sname, meta in ir["skills"].items():
                    sha = str(meta.get("commit_sha", ""))[:8] or "-"
                    assets = int(meta.get("bundled_files", 0))
                    print(
                        f"  {sname:<24} {meta.get('state', '-'):<12} {sha:<10} {assets:>6}  {meta.get('repo', '')}"
                    )
            if ir["unmanaged_skills"]:
                print(f"  Unmanaged user skills (preserved): {', '.join(ir['unmanaged_skills'])}")
            print()

        return 0
    finally:
        reg.close()


def _cmd_check_updates(args: argparse.Namespace) -> int:
    packs_cfg = load_yaml_config(args.packs_config)
    sources_cfg = load_yaml_config(args.sources_config)
    targets = _resolve_cli_targets(args, default_all_agents=True)
    if not targets:
        print("Error: specify --target <dir> or --agent {agy,claude,codex,all}.", file=sys.stderr)
        return 2

    reg = SkillRegistry(args.db)
    try:
        # Collect installed lockfiles across targets
        target_locks: list[tuple[str, Path, PackManager, dict]] = []
        packs_to_sync: set[str] = set()
        for label, target_path in targets:
            mgr = PackManager(target_path)
            lock = mgr.read_lock()
            target_locks.append((label, target_path, mgr, lock))
            pname = lock.get("pack")
            if pname and pname in packs_cfg.get("packs", {}):
                packs_to_sync.add(pname)

        if not args.no_fetch:
            if packs_to_sync:
                for pname in sorted(packs_to_sync):
                    if not args.json:
                        print(f"Checking upstream GitHub updates for pack '{pname}'...")
                    _sync_preset_skills(packs_cfg, sources_cfg, reg, args.db, pack_name=pname, fetch=True)
            else:
                if not args.json:
                    print("Checking upstream GitHub updates for all configured packs...")
                _sync_preset_skills(packs_cfg, sources_cfg, reg, args.db, pack_name="all", fetch=True)

        indexed = {s.skill_id: s for s in reg.list_skills()}
        all_updates_report: list[dict] = []
        total_available = 0
        checked_domains: set[str] = set()

        for label, target_path, mgr, lock in target_locks:
            installed_skills = lock.get("skills", {})
            active_pack = lock.get("pack", "")
            if active_pack in packs_cfg.get("packs", {}):
                dom = packs_cfg["packs"][active_pack].get("domain")
                if dom:
                    checked_domains.add(dom)

            target_entries: list[dict] = []
            for sname, meta in installed_skills.items():
                repo = meta.get("repo", "")
                spath = meta.get("path", "")
                sid = f"{repo}:{spath}"
                rec = indexed.get(sid)
                skill_file = target_path / sname / "SKILL.md"
                if not skill_file.exists() or rec is None:
                    continue

                installed_text = skill_file.read_text(encoding="utf-8")
                installed_hash = compute_content_hash(installed_text)
                upstream_hash = rec.content_hash
                state = meta.get("state", "tracked")

                if installed_hash == upstream_hash:
                    target_entries.append(
                        {
                            "skill": sname,
                            "status": "up_to_date",
                            "state": state,
                            "installed_sha": str(meta.get("commit_sha", ""))[:8],
                            "upstream_sha": rec.commit_sha[:8],
                        }
                    )
                    continue

                diff = build_semantic_diff(sname, installed_text, rec.raw_content)
                sec = scan_skill_security(rec.raw_content)
                status = "update_available" if sec.passed else "security_blocked"
                if sec.passed:
                    total_available += 1

                entry = {
                    "skill": sname,
                    "status": status,
                    "state": state,
                    "installed_sha": str(meta.get("commit_sha", ""))[:8],
                    "upstream_sha": rec.commit_sha[:8],
                    "summary": diff.summary,
                    "added_lines": diff.added_lines,
                    "removed_lines": diff.removed_lines,
                    "repo": repo,
                    "skill_id": sid,
                    "applied": None,
                }

                if args.apply and sec.passed:
                    if state == "pinned" and args.apply != "interactive" and not args.force:
                        entry["applied"] = "skipped (pinned; pass --force or use --apply interactive)"
                    else:
                        decision = args.apply
                        if decision == "interactive":
                            print(f"\n[{label}] Update available for '{sname}' ({diff.summary}, state={state})")
                            if args.show_diff:
                                print(diff.unified_diff)
                            choice = input("Choose action [replace / keep / side-by-side]: ").strip().lower()
                            decision = choice if choice in {"replace", "keep", "side-by-side"} else "keep"
                        msg = mgr.apply_update(
                            sname,
                            rec.raw_content,
                            new_commit_sha=rec.commit_sha,
                            decision=decision,
                            new_repo=rec.repo,
                            bundle_dir=bundle_cache_dir(args.db, sid),
                        )
                        entry["applied"] = msg

                target_entries.append(entry)

            all_updates_report.append(
                {
                    "agent": label,
                    "path": str(target_path),
                    "pack": active_pack or "(none)",
                    "skills": target_entries,
                }
            )

        # Also check Champion-Challenger recommendations from local registry
        champions = reg.list_skills(champions_only=True)
        candidates = reg.list_skills(candidates_only=True)
        recs = recommend_replacements(champions, candidates, config=sources_cfg)
        if checked_domains:
            recs = [r for r in recs if r.domain in checked_domains or r.domain == "base"]

        if args.json:
            print(
                json.dumps(
                    {
                        "targets": all_updates_report,
                        "updates_available": total_available,
                        "recommendations": [r.__dict__ for r in recs],
                    },
                    indent=2,
                    ensure_ascii=False,
                )
            )
            return 0

        for trep in all_updates_report:
            print(f"\nUpdate Check [{trep['agent']}] -> {trep['path']} (pack={trep['pack']})")
            if not trep["skills"]:
                print("  No skillpack-managed skills found here. Run 'skillpack assemble' first.")
                continue
            for item in trep["skills"]:
                if item["status"] == "up_to_date":
                    print(f"  [UP-TO-DATE] {item['skill']} (sha={item['installed_sha'] or '-'})")
                elif item["status"] == "security_blocked":
                    print(f"  [BLOCKED]    {item['skill']} — upstream update failed security scan!")
                else:
                    pin_tag = " [PINNED]" if item["state"] == "pinned" else ""
                    print(
                        f"  [UPDATE]{pin_tag} {item['skill']}: {item['summary']} "
                        f"({item['installed_sha'] or '-'} -> {item['upstream_sha'] or '-'})"
                    )
                    if args.show_diff and not args.apply:
                        installed_text = (Path(trep["path"]) / item["skill"] / "SKILL.md").read_text(
                            encoding="utf-8"
                        )
                        rec = indexed[item["skill_id"]]
                        print(build_semantic_diff(item["skill"], installed_text, rec.raw_content).unified_diff)
                    if item["applied"]:
                        print(f"    -> {item['applied']}")

        print("\nChampion-Challenger Recommendations:")
        if not recs:
            print("  No higher-scoring challengers exceeded the replacement margin.")
        else:
            for r in recs:
                print(f"  [{r.kind.upper()}] ({r.domain}) {r.rationale}")

        if total_available > 0 and not args.apply:
            print(
                "\nTip: run 'skillpack check-updates --no-fetch --apply interactive --show-diff' "
                "(or '--apply replace') to apply available updates."
            )
        return 0
    finally:
        reg.close()


def _cmd_discover(args: argparse.Namespace) -> int:
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
            dom_cfg = domain_queries[dom]
            if args.dry_run:
                qs = crawler.build_trending_repo_queries(
                    dom, dom_cfg, days=args.days, min_stars=args.min_stars
                )
                print(f"[DRY-RUN] Trending discovery for domain '{dom}' ({len(qs)} queries):")
                for q in qs:
                    print(f"  - {q}")
                continue

            repos = crawler.search_trending_repos(
                dom,
                dom_cfg,
                days=args.days,
                min_stars=args.min_stars,
                max_repos=args.max_repos,
            )
            print(
                f"=== Trending Discovery [{dom}] (window={args.days}d, min_stars={args.min_stars}): "
                f"found {len(repos)} breakout/surging repos ==="
            )
            ingested = 0
            blocked_count = 0
            for repo_info in repos:
                repo = repo_info["repo"]
                stars = repo_info["stars"]
                vel = repo_info["velocity_per_day"]
                age = repo_info["created_days_ago"]
                growth_90d = reg.record_repo_snapshot(repo, stars, created_days_ago=age)
                skill_paths = crawler.discover_skills_in_repo(
                    repo,
                    branch=repo_info["default_branch"],
                    domain_keywords=dom_cfg.get("keywords", []),
                    max_skills=args.max_skills_per_repo,
                )
                if not skill_paths:
                    continue
                print(
                    f"  Repo {repo} (stars={stars}, age={age}d, velocity={vel}/day, "
                    f"est_90d_growth={growth_90d}) -> {len(skill_paths)} SKILL.md candidate(s)"
                )
                for spath in skill_paths:
                    try:
                        raw_md, sha, extra_files = crawler.fetch_skill_bundle(
                            repo, spath, ref=repo_info["default_branch"], max_files=20
                        )
                    except (urllib.error.HTTPError, urllib.error.URLError):
                        continue
                    if not raw_md:
                        continue
                    has_scripts = any(p.startswith("scripts/") for p in extra_files)
                    has_refs = any(
                        p.startswith("references/") or p.lower().endswith("reference.md")
                        for p in extra_files
                    )
                    sec = scan_skill_security(
                        raw_md,
                        extra_scripts=_extract_script_texts(extra_files),
                        raw_assets=extra_files,
                    )
                    try:
                        doc = parse_skill_md(raw_md, has_scripts=has_scripts, has_references=has_refs)
                    except ValueError:
                        continue

                    sid = f"{repo}:{spath}"
                    rec = SkillRecord(
                        skill_id=sid,
                        name=doc.name,
                        description=doc.description,
                        domain=dom,
                        repo=repo,
                        path=spath,
                        url=f"{repo_info['url']}/blob/{repo_info['default_branch']}/{spath}",
                        commit_sha=sha or "trending",
                        content_hash=doc.content_hash,
                        raw_content=raw_md,
                        stars=stars,
                        stars_90d_growth=growth_90d,
                        days_since_update=int(repo_info["days_since_update"]),
                        license_spdx=str(repo_info["license_spdx"]),
                        has_scripts=has_scripts,
                        has_references=has_refs,
                        estimated_tokens=doc.estimated_tokens,
                        security_passed=sec.passed,
                        is_champion=False,
                    )
                    rec.score = evaluate_skill(rec, doc, config=sources_cfg)
                    reg.upsert_skill(rec)
                    if sec.passed:
                        save_skill_bundle(bundle_cache_dir(args.db, sid), extra_files)
                        ingested += 1
                        print(f"    [SAFE]    {doc.name} (score={rec.score.total}, assets={len(extra_files)})")
                    else:
                        blocked_count += 1
                        rules = ", ".join(sorted({f.rule_id for f in sec.findings}))
                        print(f"    [BLOCKED] {doc.name} — quarantined by security gate ({rules})")
                        for f in sec.findings[:3]:
                            print(f"              {f.severity.upper()} {f.rule_id}: {f.evidence}")

            print(
                f"Domain '{dom}' discovery complete: {ingested} safe candidate(s) indexed, "
                f"{blocked_count} quarantined."
            )
            champions = reg.list_skills(domain=dom, champions_only=True)
            candidates = reg.list_skills(domain=dom, candidates_only=True)
            recs = recommend_replacements(champions, candidates, config=sources_cfg)
            if recs:
                reg.save_recommendations(recs)
                print(f"  Breakout Recommendations ({len(recs)}):")
                for r in recs[:10]:
                    print(f"    [{r.kind.upper()}] {r.rationale}")
            else:
                print("  No new breakout challengers exceeded the replacement margin.")
        return 0
    finally:
        reg.close()


def _audit_directory(target_path: Path) -> list[dict]:
    results: list[dict] = []
    if not target_path.is_dir():
        return results
    for child in sorted(target_path.iterdir()):
        if not child.is_dir() or child.name.startswith("."):
            continue
        skill_md = child / "SKILL.md"
        if not skill_md.is_file():
            continue
        raw_md = skill_md.read_text(encoding="utf-8", errors="replace")
        raw_assets: dict[str, bytes] = {}
        for fpath in child.rglob("*"):
            if not fpath.is_file() or fpath == skill_md:
                continue
            rel = str(fpath.relative_to(child))
            try:
                raw_assets[rel] = fpath.read_bytes()
            except OSError:
                continue
        sec = scan_skill_security(
            raw_md,
            extra_scripts=_extract_script_texts(raw_assets),
            raw_assets=raw_assets,
        )
        results.append(
            {
                "skill": child.name,
                "path": str(child),
                "passed": sec.passed,
                "blocked": sec.blocked,
                "findings": [f.__dict__ for f in sec.findings],
            }
        )
    return results


def _cmd_audit(args: argparse.Namespace) -> int:
    report: dict = {"targets": [], "registry": []}
    any_blocked = False

    targets = _resolve_cli_targets(args, default_all_agents=(not args.registry))
    for label, target_path in targets:
        items = _audit_directory(target_path)
        for it in items:
            if it["blocked"]:
                any_blocked = True
        report["targets"].append({"agent": label, "path": str(target_path), "skills": items})

    if args.registry:
        reg = SkillRegistry(args.db)
        try:
            all_skills = reg.list_skills() + reg.list_blocked_skills()
            for rec in all_skills:
                bdir = bundle_cache_dir(args.db, rec.skill_id)
                raw_assets: dict[str, bytes] = {}
                if bdir.is_dir():
                    for fpath in bdir.rglob("*"):
                        if fpath.is_file():
                            raw_assets[str(fpath.relative_to(bdir))] = fpath.read_bytes()
                sec = scan_skill_security(
                    rec.raw_content,
                    extra_scripts=_extract_script_texts(raw_assets),
                    raw_assets=raw_assets,
                )
                if sec.blocked:
                    any_blocked = True
                report["registry"].append(
                    {
                        "skill_id": rec.skill_id,
                        "name": rec.name,
                        "domain": rec.domain,
                        "passed": sec.passed,
                        "blocked": sec.blocked,
                        "findings": [f.__dict__ for f in sec.findings],
                    }
                )
        finally:
            reg.close()

    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 1 if any_blocked else 0

    for trep in report["targets"]:
        skills = trep["skills"]
        blocked_n = sum(1 for s in skills if s["blocked"])
        warn_n = sum(1 for s in skills if not s["blocked"] and s["findings"])
        print(
            f"Security Audit [{trep['agent']}] -> {trep['path']} "
            f"(scanned={len(skills)}, blocked={blocked_n}, warnings={warn_n})"
        )
        for s in skills:
            if s["blocked"]:
                print(f"  [BLOCKED] {s['skill']}")
                for f in s["findings"]:
                    print(f"    - {f['severity'].upper()} {f['rule_id']}: {f['description']} ({f['evidence']})")
            elif s["findings"]:
                print(f"  [WARN]    {s['skill']}")
                for f in s["findings"]:
                    print(f"    - {f['severity'].upper()} {f['rule_id']}: {f['description']} ({f['evidence']})")
            else:
                print(f"  [CLEAN]   {s['skill']}")

    if args.registry:
        reg_items = report["registry"]
        blocked_n = sum(1 for s in reg_items if s["blocked"])
        warn_n = sum(1 for s in reg_items if not s["blocked"] and s["findings"])
        print(
            f"\nRegistry Security Audit ({args.db}): "
            f"scanned={len(reg_items)}, blocked={blocked_n}, warnings={warn_n}"
        )
        for s in reg_items:
            if s["blocked"] or s["findings"]:
                tag = "BLOCKED" if s["blocked"] else "WARN"
                print(f"  [{tag}] {s['name']} ({s['skill_id']})")
                for f in s["findings"]:
                    print(f"    - {f['severity'].upper()} {f['rule_id']}: {f['description']} ({f['evidence']})")

    return 1 if any_blocked else 0


def _cmd_expand(args: argparse.Namespace) -> int:
    packs_cfg = load_yaml_config(args.packs_config)
    sources_cfg = load_yaml_config(args.sources_config)
    packs = packs_cfg.get("packs", {})

    if args.pack == "all":
        target_packs = [
            pname
            for pname, pcfg in packs.items()
            if pcfg.get("domain") != "base"
            and (pcfg.get("auto_expand") or {}).get("enabled", True) is not False
        ]
    else:
        if args.pack not in packs:
            print(f"Unknown pack '{args.pack}'. Available: {sorted(packs.keys())}", file=sys.stderr)
            return 2
        target_packs = [args.pack]

    # Optional live dynamic trending discovery across every target pack's domain first
    if args.discover:
        domains = sorted({str(packs[p].get("domain", "")) for p in target_packs if packs[p].get("domain")})
        for dom in domains:
            if dom not in sources_cfg.get("domain_queries", {}):
                continue
            disc_ns = argparse.Namespace(
                db=args.db,
                packs_config=args.packs_config,
                sources_config=args.sources_config,
                domain=dom,
                days=args.days,
                min_stars=args.min_stars,
                max_repos=args.max_repos,
                max_skills_per_repo=args.max_skills_per_repo,
                dry_run=False,
            )
            _cmd_discover(disc_ns)

    reg = SkillRegistry(args.db)
    try:
        champions = {s.skill_id: s for s in reg.list_skills(champions_only=True)}
        all_candidates = reg.list_skills(candidates_only=True)

        report: list[dict] = []
        approved_all = []
        changed_packs: list[str] = []

        for pname in target_packs:
            proposals = plan_pack_expansion(
                packs_cfg,
                sources_cfg,
                pname,
                champions_by_id=champions,
                candidates=all_candidates,
                min_score_override=args.min_score,
                max_add_override=args.max_add,
                allow_replace=bool(args.allow_replace),
            )
            pack_approved = []
            for prop in proposals:
                if args.auto:
                    pack_approved.append(prop)
                elif args.interactive:
                    print(
                        f"\n[{pname}] [{prop.action.upper()}] {prop.name} ({prop.repo}) — "
                        f"score={prop.score:.1f}, stars={prop.stars}, 90d_growth={prop.stars_90d_growth}"
                    )
                    print(f"  {prop.rationale}")
                    ans = input("Promote into pack? [y/N]: ").strip().lower()
                    if ans in {"y", "yes"}:
                        pack_approved.append(prop)

            if pack_approved:
                changed_packs.append(pname)
                approved_all.extend(pack_approved)

            report.append(
                {
                    "pack": pname,
                    "domain": packs[pname].get("domain"),
                    "proposals": [p.__dict__ for p in proposals],
                    "applied_count": len(pack_approved),
                }
            )

        if approved_all:
            apply_pack_expansion(packs_cfg, approved_all)
            save_yaml_config(args.packs_config, packs_cfg)
            # Promote approved records to champions in SQLite
            by_id = {c.skill_id: c for c in all_candidates}
            for prop in approved_all:
                rec = by_id.get(prop.skill_id)
                if rec:
                    rec.is_champion = True
                    rec.slot = prop.slot
                    reg.upsert_skill(rec)

        if args.json:
            print(
                json.dumps(
                    {"packs": report, "total_applied": len(approved_all)},
                    indent=2,
                    ensure_ascii=False,
                )
            )
        else:
            for item in report:
                print(
                    f"\nAuto-Expand [{item['pack']}] (domain={item['domain']}): "
                    f"{len(item['proposals'])} eligible candidate(s), {item['applied_count']} applied"
                )
                if not item["proposals"]:
                    print("  No new security-verified, non-colliding candidates exceeded the expansion threshold.")
                for p in item["proposals"]:
                    print(
                        f"  [{p['action'].upper()}] {p['name']} ({p['repo']}) — "
                        f"score={p['score']:.1f}, stars={p['stars']}, 90d_growth={p['stars_90d_growth']}"
                    )
                    print(f"    {p['rationale']}")
            if approved_all:
                print(f"\nUpdated {args.packs_config} with {len(approved_all)} promoted skill(s).")
            elif any(item["proposals"] for item in report):
                print(
                    "\nPreview only. Pass '--auto' or '--interactive' to promote these skills into packs.yaml."
                )

        # Optionally re-assemble changed packs directly to target agents
        if approved_all and (args.agent or args.target):
            for pname in changed_packs:
                asm_ns = argparse.Namespace(
                    db=args.db,
                    packs_config=args.packs_config,
                    sources_config=args.sources_config,
                    pack=pname,
                    target=args.target,
                    agent=args.agent,
                    scope=args.scope,
                    project_root=args.project_root,
                    fetch=False,
                    prune=False,
                )
                _cmd_assemble(asm_ns)

        return 0
    finally:
        reg.close()


def _cmd_dashboard(args: argparse.Namespace) -> int:
    import importlib.util

    packs_cfg_path = Path(args.packs_config).resolve()
    repo_root = packs_cfg_path.parents[1] if packs_cfg_path.exists() else Path.cwd()
    script_path = repo_root / "skills" / "project-command-center" / "scripts" / "serve_dashboard.py"
    if not script_path.is_file():
        print(f"Dashboard script not found at {script_path}", file=sys.stderr)
        return 1

    spec = importlib.util.spec_from_file_location("serve_dashboard", script_path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    argv = [
        "--hub", args.hub,
        "--project-root", args.project_root,
        "--agent", args.agent,
        "--host", args.host,
        "--port", str(args.port),
    ]
    if args.codex_model:
        argv.extend(["--codex-model", args.codex_model])
    if args.open:
        argv.append("--open")
    if args.init_sample:
        argv.append("--init-sample")
    return int(mod.main(argv))


def _cmd_launch(args: argparse.Namespace) -> int:
    """One-command interactive assistant launcher: assemble pack + boot agy/claude/codex + visual UI."""
    import shutil
    import subprocess
    import threading
    import webbrowser

    project_root = Path(args.project_root).expanduser().resolve()
    project_root.mkdir(parents=True, exist_ok=True)
    hub_dir = Path(args.hub) if args.hub else (project_root / "project-hub")

    # 1. Assemble requested skill pack into the project's native skill dirs so the chosen CLI loads them
    asm_ns = argparse.Namespace(
        db=args.db,
        packs_config=args.packs_config,
        sources_config=args.sources_config,
        pack=args.pack,
        target=None,
        agent="all",
        scope="project",
        project_root=str(project_root),
        fetch=bool(args.fetch),
        prune=bool(args.prune),
    )
    rc = _cmd_assemble(asm_ns)
    if rc != 0:
        return rc

    # 2. Load dashboard/assistant module
    import importlib.util

    packs_cfg_path = Path(args.packs_config).resolve()
    repo_root = packs_cfg_path.parents[1] if packs_cfg_path.exists() else Path.cwd()
    script_path = repo_root / "skills" / "project-command-center" / "scripts" / "serve_dashboard.py"
    spec = importlib.util.spec_from_file_location("serve_dashboard", script_path)
    assert spec and spec.loader
    dash_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(dash_mod)

    if args.init_sample or (args.pack == "project-mgmt" and not hub_dir.exists()):
        dash_mod.init_sample_hub(hub_dir)

    # 3a. Terminal interactive mode: start visual board in background + launch interactive agy/claude/codex TTY
    if args.terminal:
        binary = shutil.which(args.agent)
        if not binary:
            print(f"Error: '{args.agent}' CLI not found in PATH.", file=sys.stderr)
            return 1
        srv = dash_mod.create_server(
            hub_dir,
            host=args.host,
            port=args.port,
            project_root=project_root,
            default_agent=args.agent,
            codex_model=args.codex_model,
        )
        actual_port = srv.server_address[1]
        url = f"http://{args.host}:{actual_port}"
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        print(f"Visual PM Board running at {url} — launching interactive '{args.agent}' session...")
        if args.open:
            webbrowser.open(url)
        try:
            cmd = [binary]
            if args.agent == "codex" and args.codex_model:
                cmd.extend(["-m", args.codex_model])
            return int(subprocess.call(cmd, cwd=str(project_root)))
        finally:
            srv.shutdown()
            srv.server_close()

    # 3b. Default: Launch Split-Screen Interactive Assistant Web UI powered by agy / claude / codex
    dash_ns = argparse.Namespace(
        packs_config=args.packs_config,
        hub=str(hub_dir),
        project_root=str(project_root),
        agent=args.agent,
        codex_model=args.codex_model,
        host=args.host,
        port=args.port,
        open=bool(args.open),
        init_sample=False,
    )
    return _cmd_dashboard(dash_ns)


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

    p_show = sub.add_parser("show", help="Inspect skills inside configured packs or installed agent directories.")
    p_show.add_argument("--pack", help="Show details for a specific pack from packs.yaml.")
    p_show.add_argument("--target", help="Inspect installed skills in a custom target directory.")
    p_show.add_argument(
        "--agent",
        choices=["agy", "claude", "codex", "all"],
        help="Inspect installed skills in native directory for agy, claude, codex, or all.",
    )
    p_show.add_argument("--scope", choices=["project", "global"], default="project")
    p_show.add_argument("--project-root", default=".")
    p_show.add_argument("--json", action="store_true", help="Output JSON.")
    p_show.set_defaults(func=_cmd_show)

    p_chk = sub.add_parser(
        "check-updates",
        help="Check installed skills against upstream GitHub versions and Challenger recommendations.",
    )
    p_chk.add_argument("--target", help="Check a custom assembled pack directory.")
    p_chk.add_argument(
        "--agent",
        choices=["agy", "claude", "codex", "all"],
        help="Check native directory for agy, claude, codex, or all (default: all when --target omitted).",
    )
    p_chk.add_argument("--scope", choices=["project", "global"], default="project")
    p_chk.add_argument("--project-root", default=".")
    p_chk.add_argument(
        "--no-fetch",
        action="store_true",
        help="Compare against local registry without fetching from GitHub.",
    )
    p_chk.add_argument("--show-diff", action="store_true", help="Print unified diff for available updates.")
    p_chk.add_argument(
        "--apply",
        choices=["interactive", "replace", "keep", "side-by-side"],
        help="Apply available upstream updates directly or interactively.",
    )
    p_chk.add_argument(
        "--force",
        action="store_true",
        help="Allow non-interactive --apply replace to update pinned skills.",
    )
    p_chk.add_argument("--json", action="store_true", help="Output JSON.")
    p_chk.set_defaults(func=_cmd_check_updates)

    p_crawl = sub.add_parser("crawl", help="Crawl GitHub for SKILL.md candidates by domain.")
    p_crawl.add_argument("--domain", default="all", help="Domain name or 'all'.")
    p_crawl.add_argument("--limit", type=int, default=30, help="Max items per domain.")
    p_crawl.add_argument("--dry-run", action="store_true", help="Print partitioned queries without network calls.")
    p_crawl.set_defaults(func=_cmd_crawl)

    p_disc = sub.add_parser(
        "discover",
        help="Dynamically discover newly exploding / trending GitHub skill repos and quarantine unsafe skills.",
    )
    p_disc.add_argument("--domain", default="all", help="Domain name or 'all'.")
    p_disc.add_argument("--days", type=int, default=90, help="Rolling lookback window in days for breakout repos.")
    p_disc.add_argument("--min-stars", type=int, default=25, help="Minimum GitHub stars threshold.")
    p_disc.add_argument("--max-repos", type=int, default=10, help="Max trending repos to inspect per domain.")
    p_disc.add_argument("--max-skills-per-repo", type=int, default=5, help="Max SKILL.md files to inspect per repo.")
    p_disc.add_argument("--dry-run", action="store_true", help="Print trending search queries without network calls.")
    p_disc.set_defaults(func=_cmd_discover)

    p_audit = sub.add_parser(
        "audit",
        help="Deep security & prompt-injection audit over installed skills or the local registry.",
    )
    p_audit.add_argument("--target", help="Audit a custom skills directory.")
    p_audit.add_argument(
        "--agent",
        choices=["agy", "claude", "codex", "all"],
        help="Audit native directory for agy, claude, codex, or all.",
    )
    p_audit.add_argument("--scope", choices=["project", "global"], default="project")
    p_audit.add_argument("--project-root", default=".")
    p_audit.add_argument("--registry", action="store_true", help="Also audit all skills stored in SQLite registry.")
    p_audit.add_argument("--json", action="store_true", help="Output JSON.")
    p_audit.set_defaults(func=_cmd_audit)

    p_exp = sub.add_parser(
        "expand",
        help="Auto-expand any or all skill packs with high-scoring, security-clean, non-colliding skills.",
    )
    p_exp.add_argument("--pack", default="all", help="Pack name from packs.yaml or 'all' (default: all).")
    p_exp.add_argument(
        "--discover",
        action="store_true",
        help="Run dynamic trending GitHub repo discovery across target pack domains before expanding.",
    )
    p_exp.add_argument("--days", type=int, default=90, help="Lookback window when using --discover.")
    p_exp.add_argument("--min-stars", type=int, default=25, help="Min stars when using --discover.")
    p_exp.add_argument("--max-repos", type=int, default=6, help="Max trending repos per domain with --discover.")
    p_exp.add_argument("--max-skills-per-repo", type=int, default=4, help="Max skills per repo with --discover.")
    p_exp.add_argument("--min-score", type=float, help="Override pack min_score threshold.")
    p_exp.add_argument("--max-add", type=int, help="Override pack max_add_per_run cap.")
    p_exp.add_argument("--allow-replace", action="store_true", help="Also allow replacing weaker champions.")
    p_exp.add_argument("--auto", action="store_true", help="Automatically promote eligible skills into packs.yaml.")
    p_exp.add_argument("--interactive", action="store_true", help="Prompt before promoting each candidate.")
    p_exp.add_argument("--target", help="Optionally re-assemble updated pack into custom directory.")
    p_exp.add_argument("--agent", choices=["agy", "claude", "codex", "all"], help="Optionally re-assemble to agents.")
    p_exp.add_argument("--scope", choices=["project", "global"], default="project")
    p_exp.add_argument("--project-root", default=".")
    p_exp.add_argument("--json", action="store_true", help="Output JSON.")
    p_exp.set_defaults(func=_cmd_expand)

    p_dash = sub.add_parser(
        "dashboard",
        help="Launch the interactive PM Assistant (agy/claude/codex) + live visual workforce dashboard.",
    )
    p_dash.add_argument("--hub", default="./project-hub", help="Path to project-hub directory.")
    p_dash.add_argument("--project-root", default=".", help="Workspace root where agy/claude/codex runs.")
    p_dash.add_argument(
        "--agent",
        choices=["agy", "claude", "codex"],
        default="agy",
        help="Default AI engine powering the interactive assistant (default: agy).",
    )
    p_dash.add_argument("--codex-model", help="Optional model override for codex exec.")
    p_dash.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1).")
    p_dash.add_argument("--port", type=int, default=8765, help="Bind port (default: 8765).")
    p_dash.add_argument("--open", action="store_true", help="Open browser automatically on startup.")
    p_dash.add_argument(
        "--init-sample",
        action="store_true",
        help="Initialize starter project-hub/ files if missing.",
    )
    p_dash.set_defaults(func=_cmd_dashboard)

    p_launch = sub.add_parser(
        "launch",
        help="Assemble a skill pack and launch an interactive assistant powered by agy, claude, or codex.",
    )
    p_launch.add_argument("--pack", default="project-mgmt", help="Pack to assemble (default: project-mgmt).")
    p_launch.add_argument(
        "--agent",
        choices=["agy", "claude", "codex"],
        default="agy",
        help="AI CLI engine to launch (agy, claude, or codex).",
    )
    p_launch.add_argument("--project-root", default=".", help="Target project workspace directory.")
    p_launch.add_argument("--hub", help="Optional custom project-hub path (default: <project-root>/project-hub).")
    p_launch.add_argument("--codex-model", help="Optional model override for codex.")
    p_launch.add_argument("--host", default="127.0.0.1")
    p_launch.add_argument("--port", type=int, default=8765)
    p_launch.add_argument("--open", action="store_true", help="Open the interactive visual assistant in browser.")
    p_launch.add_argument(
        "--terminal",
        action="store_true",
        help="Open interactive agy/claude/codex directly in terminal while running visual board in background.",
    )
    p_launch.add_argument("--fetch", action="store_true", help="Fetch upstream skills if not yet synced.")
    p_launch.add_argument("--prune", action="store_true", help="Remove previously managed skills when switching pack.")
    p_launch.add_argument("--init-sample", action="store_true", help="Initialize starter project-hub/ if missing.")
    p_launch.set_defaults(func=_cmd_launch)

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
