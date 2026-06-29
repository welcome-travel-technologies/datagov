"""
Extract dbt artifacts from a GitHub repository.

Clones the repo (shallow, single branch), locates manifest.json and
catalog.json, then runs the transform step to produce catalog CSVs.
"""
import os
import sys
import shutil
import subprocess
import glob


def run_dbt_extraction(github_repo_url, github_token, github_branch,
                       dbt_manifest_path, etl_dir, log):
    """
    Main entry point called by integration_tasks.run_source_task().

    1. Clones the GitHub repo into etl_dir/dbt_artifacts/repo/
    2. Locates manifest.json and catalog.json
    3. Calls transform_dbt.main() to parse and produce CSVs
    """
    if not github_repo_url:
        raise ValueError('Missing GitHub repository URL')

    branch = github_branch or 'main'
    manifest_path = dbt_manifest_path or 'target/manifest.json'

    artifacts_dir = os.path.join(etl_dir, 'dbt_artifacts')
    repo_dir = os.path.join(artifacts_dir, 'repo')

    # Clean previous clone
    if os.path.exists(repo_dir):
        shutil.rmtree(repo_dir)
    os.makedirs(repo_dir, exist_ok=True)

    # Build the clone URL — embed the PAT as TOKEN:x-oauth-basic which is the
    # official GitHub basic-auth PAT format, works with all git versions.
    # Strip any trailing slash from the URL as some git versions choke on it.
    base_url = github_repo_url.rstrip('/')
    if github_token and base_url.startswith('https://'):
        host_path = base_url[len('https://'):]
        if '@' in host_path:
            host_path = host_path.split('@', 1)[1]
        clone_url = f'https://{github_token}:x-oauth-basic@{host_path}'
    else:
        clone_url = base_url

    log(f'Cloning repository: {github_repo_url} (branch: {branch})...')
    try:
        subprocess.run(
            ['git', 'clone', '--depth', '1', '--branch', branch,
             '--single-branch', clone_url, repo_dir],
            check=True,
            capture_output=True,
            text=True,
            timeout=300,
            stdin=subprocess.DEVNULL,
            env={**os.environ, 'GIT_TERMINAL_PROMPT': '0'},
        )
    except subprocess.CalledProcessError as e:
        # Sanitise error output to avoid leaking tokens
        stderr = (e.stderr or '').replace(github_token, '***') if github_token else (e.stderr or '')
        raise ValueError(f'Git clone failed: {stderr}')
    except subprocess.TimeoutExpired:
        raise ValueError('Git clone timed out after 300 seconds')

    log('✅ Repository cloned successfully.')

    # ── Locate manifest.json ──────────────────────────────────────────────
    full_manifest_path = os.path.join(repo_dir, manifest_path)
    if not os.path.exists(full_manifest_path):
        # Auto-discover: search the entire repo for manifest.json
        log(f'manifest.json not found at configured path "{manifest_path}". Searching repo...')
        candidates = glob.glob(os.path.join(repo_dir, '**', 'manifest.json'), recursive=True)
        if not candidates:
            raise ValueError(
                f'manifest.json not found anywhere in the repository. '
                f'Run "dbt compile" or "dbt run" to generate it, then commit it to the repo.'
            )
        # Prefer target/manifest.json if multiple found, otherwise take the first
        preferred = [c for c in candidates if os.sep + 'target' + os.sep in c or '/target/' in c]
        full_manifest_path = preferred[0] if preferred else candidates[0]
        found_rel = os.path.relpath(full_manifest_path, repo_dir)
        log(f'✅ Auto-discovered manifest.json at: {found_rel}')
    else:
        log(f'Found manifest.json at: {manifest_path}')

    # ── Locate catalog.json (optional, same directory as manifest) ────────
    manifest_dir = os.path.dirname(full_manifest_path)
    full_catalog_path = os.path.join(manifest_dir, 'catalog.json')
    if not os.path.exists(full_catalog_path):
        # Also try auto-discovering catalog.json in the repo
        catalog_candidates = glob.glob(
            os.path.join(repo_dir, '**', 'catalog.json'), recursive=True,
        )
        if catalog_candidates:
            preferred_cat = [
                c for c in catalog_candidates
                if os.sep + 'target' + os.sep in c or '/target/' in c
            ]
            full_catalog_path = preferred_cat[0] if preferred_cat else catalog_candidates[0]
            cat_rel = os.path.relpath(full_catalog_path, repo_dir)
            log(f'✅ Auto-discovered catalog.json at: {cat_rel}')
        else:
            full_catalog_path = None
            log('ℹ️  catalog.json not found — using manifest.json only. '
                'Run "dbt docs generate" to produce catalog.json for richer column metadata.')
    else:
        cat_rel = os.path.relpath(full_catalog_path, repo_dir)
        log(f'Found catalog.json at: {cat_rel}')

    # ── Run transform ─────────────────────────────────────────────────────
    log('\nRunning dbt transform...')

    if etl_dir not in sys.path:
        sys.path.append(etl_dir)

    import transform_dbt

    class LogCapture:
        def write(self, text):
            if text.strip():
                log(f'    {text.strip()}')
        def flush(self):
            pass

    old_stdout = sys.stdout
    sys.stdout = LogCapture()

    try:
        transform_dbt.main(
            manifest_path=full_manifest_path,
            repo_dir=repo_dir,
            output_dir=os.path.join(etl_dir, 'data'),
            catalog_path=full_catalog_path,
        )
    finally:
        sys.stdout = old_stdout

    log('✅ dbt transform complete.')
