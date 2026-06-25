import argparse, os, sys, shlex, tempfile, shutil, json, time, re, pwd, subprocess, zipfile
import urllib.request, urllib.error
from typing import List, Dict, Optional
from pathlib import Path
from dataclasses import dataclass
from enum import Enum
from collections import defaultdict

DEFAULT_ENV_PATH = "tasks"
DEFAULT_TIME_LIMIT = 1.0 # seconds
DEFAULT_MEMORY_LIMIT = 64 # MB
DEFAULT_LANGUAGE = "c"
DEFAULT_EXAM_TEMPLATE_STUDENT = "ImePrezime_mxGGBBB"
DEFAULT_EXAM_TEMPLATE_TERM = "jan1_"
DEFAULT_EXAM_TEMPLATE_COURSE = "KiAA_"
CURRENT_DIR = "."
BUILD_DIR = "_build"
TEST_DIR = "tests"
VERSION = "0.0.1"
PACKAGE_ROOT = Path(__file__).resolve().parent
STATIC_DIR = PACKAGE_ROOT / "static"
GITHUB_MARKDOWN_CSS = STATIC_DIR / "github-markdown.css"
TEX_MML_CHTML_JS = STATIC_DIR / "tex-mml-chtml.js"
TESTER_PY = STATIC_DIR / "tester.py"

@dataclass
class TestCase:
    tid: str
    fin: Path | None
    fout: Path | None
    ferr: Path | None
    fexit: Path | None
    fargs: Path | None
    extra_ins: list[Path]
    extra_outs: list[Path]

@dataclass
class ProblemMeta:
    pid: str
    pname: str
    time_limit_ms: float
    memory_limit_mb: int 
    language: str
    sources: List[Path]
    headers: List[Path]

class TestResult(Enum):
    OK = "OK"
    RTE = "RTE"
    TLE = "TLE"
    WA = "WA"

def normalize_text(s: str) -> str:
    if s is None:
        return ""

    # normalize newlines to \n first
    s = s.replace('\r\n', '\n').replace('\r', '\n')

    # collapse any run of whitespace (spaces, tabs, newlines) into a single space
    s = re.sub(r"\s+", " ", s)

    # trim leading/trailing spaces
    return s.strip()

def get_user() -> str:
    return pwd.getpwuid(os.getuid())[0]

def discover_tests(tdir : Path) -> list[TestCase]:
    if not tdir.exists():
        print(f'Error: {tdir} not found!', file=sys.stderr)
        SystemExit(2)

    by_id: dict[str, dict[str, list[Path] | Path]] = {}
    rx = re.compile(r"^(\d+)\.(.+)$")

    for path in tdir.iterdir():
        if not path.is_file():
            continue
        m = rx.match(path.name)
        if not m:
            continue
        tid, rest = m.group(1), m.group(2)
        rec = by_id.setdefault(tid, {
            'fin': None, 'fout': None, 'ferr': None, 'fargs': None, 'fexit': None,
            'extra_ins': [], 'extra_outs': []}
        )
        
        if rest == 'in':
            rec['fin'] = path
        elif rest == 'out':
            rec['fout'] = path
        elif rest == 'err':
            rec['ferr'] = path
        elif rest == 'args':
            rec['fargs'] = path
        elif rest == 'exit':
            rec['fexit'] = path
        elif rest.startswith('ins'):
            rec['extra_ins'].append(path)
        elif rest.startswith('outs'):
            rec['extra_outs'].append(path)
        else:
            pass

    tests : list[TestCase] = []

    for tid, rec in sorted(by_id.items(), key=lambda kv: int(kv[0])):
        tests.append(TestCase(
            tid=tid,
            fin=rec['fin'],
            fout=rec['fout'],
            ferr=rec['ferr'],
            fexit=rec['fexit'],
            fargs=rec['fargs'],
            extra_ins=sorted(rec['extra_ins']),
            extra_outs=sorted(rec['extra_outs'])
        ))

    return tests

def get_problem_meta(task_dir: Path, solution_dir: Path | None = None) -> ProblemMeta | None: 
    md_file: Path | None = None
    for path in task_dir.iterdir():
        if path.is_file() and path.suffix == ".md":
            md_file = path
            break

    if md_file is None:
        print(f"Markdown file (.md) not found in {task_dir}.", file=sys.stderr)
        return None 

    pid = task_dir.name.split("_", 1)[0]
    # read metadata from md_file
    time_limit: float = DEFAULT_TIME_LIMIT
    memory_limit: int = DEFAULT_MEMORY_LIMIT 
    sources: List[Path] = []
    headers: List[Path] = []
    language: str = "c"

    with md_file.open("r", encoding="utf-8") as f:
        in_meta = False
        for line in f:
            line = line.strip()
            if line == "---":
                if not in_meta:
                    in_meta = True
                else:
                    break
            elif in_meta:
                if line.startswith("time_limit:"):
                    try:
                        time_limit = float(line.split(":", 1)[1].strip())
                    except ValueError:
                        print(f"Invalid time_limit value in {md_file}.", file=sys.stderr)
                        return None
                elif line.startswith("memory_limit:"):
                    try:
                        memory_limit = int(line.split(":", 1)[1].strip())
                    except ValueError:
                        print(f"Invalid memory_limit value in {md_file}.", file=sys.stderr)
                        return None
                elif line.startswith("sources:"):
                    srcs_str = line.split(":", 1)[1].strip().lstrip("[").rstrip("]")
                    if solution_dir is not None:
                        sources = [solution_dir / src.strip() for src in srcs_str.split(",") if src.strip()]
                    else:
                        sources = [task_dir / src.strip() for src in srcs_str.split(",") if src.strip()]
                elif line.startswith("headers:"):
                    hdrs_str = line.split(":", 1)[1].strip().lstrip("[").rstrip("]")
                    if solution_dir is not None:
                        headers = [solution_dir / hdr.strip() for hdr in hdrs_str.split(",") if hdr.strip()]
                    else:
                        headers = [task_dir / hdr.strip() for hdr in hdrs_str.split(",") if hdr.strip()]
                elif line.startswith("language:") or line.startswith("lang:"):
                    lang_val = line.split(":", 1)[1].strip().lower()
                    if lang_val in ("c", "cpp"):
                        language = lang_val
                    else:
                        print(f"Invalid language value '{lang_val}' in {md_file}. Allowed: c, cpp", file=sys.stderr)
                        return None

    return ProblemMeta(
        pid=pid,
        pname=md_file.stem,
        time_limit_ms=time_limit * 1000.0,
        memory_limit_mb=memory_limit,
        language=language,
        sources=sources,
        headers=headers
    )

def compile_cpp(problem: ProblemMeta, task_dir: Path) -> Path | None:
    compile_cmd = ["g++", "-std=c++17", "-Wall", "-Wextra", "-pedantic"]
    compile_cmd.extend(str(src) for src in problem.sources)
    compile_cmd.extend(str(hdr) for hdr in problem.headers)

    build_dir = task_dir / BUILD_DIR
    output = build_dir / f"{problem.pid}_exe"
    if not build_dir.exists():
        build_dir.mkdir(parents=True, exist_ok=True)

    compile_cmd.extend(["-o", str(output)])

    compile_proc = subprocess.run(compile_cmd, capture_output=True, text=True)
    if compile_proc.returncode != 0:
        print("Compilation failed:", file=sys.stderr)
        if compile_proc.stdout:
            print(compile_proc.stdout, file=sys.stderr)
        if compile_proc.stderr:
            print(compile_proc.stderr, file=sys.stderr)
        return None
    return output

def compile_c(problem: ProblemMeta, task_dir: Path) -> Path | None:
    compile_cmd = ["gcc", "-Wall", "-Wextra", "-pedantic"]
    compile_cmd.extend(str(src) for src in problem.sources)
    compile_cmd.extend(str(hdr) for hdr in problem.headers)

    build_dir = task_dir / BUILD_DIR
    output = build_dir / f"{problem.pid}_exe"
    if not build_dir.exists():
        build_dir.mkdir(parents=True, exist_ok=True)

    compile_cmd.extend(["-o", str(output)])

    compile_proc = subprocess.run(compile_cmd, capture_output=True, text=True)
    if compile_proc.returncode != 0:
        print("Compilation failed:", file=sys.stderr)
        if compile_proc.stdout:
            print(compile_proc.stdout, file=sys.stderr)
        if compile_proc.stderr:
            print(compile_proc.stderr, file=sys.stderr)
        return None
    return output

def build_exe(problem: ProblemMeta, task_dir: Path) -> Path | None:
    if problem.language == "c":
        return compile_c(problem, task_dir)
    elif problem.language == "cpp":
        return compile_cpp(problem, task_dir)
    else:
        print(f"Unsupported language: {problem.language}", file=sys.stderr)
        return None

def run_test_case(pmeta: ProblemMeta, tcase: TestCase, exe_path: Path, verbose: bool = False) -> TestResult:
    with tempfile.TemporaryDirectory(prefix="yacpit_") as tmpdir:
        work_dir = Path(tmpdir)
        binary_path = work_dir / exe_path.name
        shutil.copy2(exe_path, binary_path)
        binary_path.chmod(0o755)

        for extra_in in tcase.extra_ins:
            suffix_split = extra_in.name.split(".ins.", 1)
            target_name = suffix_split[1] if len(suffix_split) == 2 else extra_in.name
            shutil.copyfile(extra_in, work_dir / target_name)

        cmd: list[str] = [str(binary_path)]
        if tcase.fargs and tcase.fargs.exists():
            cmd.extend(shlex.split(tcase.fargs.read_text(encoding="utf-8"), posix=True))

        stdin_handle = open(tcase.fin, "rb") if tcase.fin and tcase.fin.exists() else None
        try:
            run_proc = subprocess.run(
                cmd,
                stdin=stdin_handle if stdin_handle else None,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=work_dir,
                timeout=pmeta.time_limit_ms / 1000.0,
                text=True,
            )
        except subprocess.TimeoutExpired:
            print(f'Test {tcase.tid}: {TestResult.TLE.name}')
            return TestResult.TLE
        finally:
            if stdin_handle:
                stdin_handle.close()

        expected_out = tcase.fout.read_text(encoding="utf-8") if tcase.fout and tcase.fout.exists() else ""
        expected_err = tcase.ferr.read_text(encoding="utf-8") if tcase.ferr and tcase.ferr.exists() else ""

        norm_expected_out = normalize_text(expected_out)
        norm_expected_err = normalize_text(expected_err)
        norm_stdout = normalize_text(run_proc.stdout)
        norm_stderr = normalize_text(run_proc.stderr)

        stdout_ok = norm_stdout == norm_expected_out
        stderr_ok = norm_stderr == norm_expected_err

        extra_ok = True
        extra_messages: list[str] = []
        for extra_out in tcase.extra_outs:
            suffix_split = extra_out.name.split(".outs.", 1)
            target_name = suffix_split[1] if len(suffix_split) == 2 else extra_out.name
            produced = work_dir / target_name
            if not produced.exists():
                extra_ok = False
                extra_messages.append(f'missing file {target_name}')
                continue
            expected_extra = extra_out.read_text(encoding="utf-8")
            actual_extra = produced.read_text(encoding="utf-8")
            if normalize_text(actual_extra) != normalize_text(expected_extra):
                extra_ok = False
                extra_messages.append(f'file {target_name} differs')

        if run_proc.returncode != 0:
            if tcase.fexit is not None and tcase.fexit.exists():
                try:
                    expected_code = int(tcase.fexit.read_text(encoding="utf-8").strip())
                except Exception:
                    expected_code = None
                if expected_code is not None and run_proc.returncode != expected_code:
                    if (verbose):
                        print(f'Test {tcase.tid}: WA')
                        print(f'  exit code mismatch: expected {expected_code}, got {run_proc.returncode}')
                    return TestResult.WA

        if stdout_ok and stderr_ok and extra_ok:
            if verbose:
                print(f'Test {tcase.tid}: OK')
            return TestResult.OK
        else:
            if verbose:
                print(f'Test {tcase.tid}: WA')
                if not stdout_ok:
                    print("  stdout mismatch")
                if not stderr_ok:
                    print("  stderr mismatch")
                if not extra_ok:
                    for msg in extra_messages:
                        print(f"  {msg}")
            return TestResult.WA

def init_env(args):
    path = Path(args.path)
    try:
        path.mkdir()
        if args.verbose:
            print(f'New environment initialized in {path}.')
    except FileExistsError:
        print(f'Error: {path} already exists', file=sys.stderr)
    except Exception as e:
        print(f'Error: {path}: {e}', file=sys.stderr)
    
def init_task(args):
    # print(args)  # Debug statement removed for production
    safe_task_name = re.sub(r'[^A-Za-z0-9_-]', '_', args.task_name)
    path = Path(args.path) / (args.task_num + "_" + safe_task_name)
    try:
        path.mkdir()
        if args.verbose:
            print(f'New directory created on {path}')
    except FileExistsError:
         print(f'Error: {path} already exists', file=sys.stderr)
    except Exception as e:
        print(f'Error: {path}: {e}', file=sys.stderr)

    path_tests = path / "tests"
    try:
        path_tests.mkdir()
        if args.verbose:
            print(f'New directory created on {path_tests}')
    except FileExistsError:
        print(f'Error: {path_tests} already exists', file=sys.stderr)
        print(f'Error: {path_tests}: {e}', file=sys.stderr)
        print(f'Error: {path_test}: {e}', file=sys.stderr)

    path_main = path / "main.c"
    try:
        path_main.touch()
        path_main.write_text(
            '#include <stdio.h>\n'
            '\n'
            'int main(void)\n'
            '{\n'
            '\tputs("Hello, world!");\n'
            '\n'
            '\treturn 0;\n'
            '}\n'
            , encoding='utf-8'
        )
        if args.verbose:
            print(f'main.c created in {path}')
    except FileExistsError:
        print(f'Error: {path_main} already exists', file=sys.stderr)
    except Exception as e:
        print(f'Error: {path_main}: {e}', file=sys.stderr)

    path_md = path / (safe_task_name + ".md")
    try:
        path_md.touch()
        path_md.write_text(
            f'---\n'
            f'title: {args.task_name}\n'
            f'time_limit: {DEFAULT_TIME_LIMIT}\n'
            f'memory_limit: {DEFAULT_MEMORY_LIMIT}\n'
            f'language: {DEFAULT_LANGUAGE}\n'
            f'sources: [ main.c ]\n'
            f'headers: [ ]\n'
            f'author: {get_user()}\n'
            f'---\n'
            f'\n'
            f'# Naslov\n'
            f'\n'
            f'Tekst zadatka\n'
            f'\n'
            f'## Ulaz\n'
            f'\n'
            f'Opis ulaza\n'
            f'\n'
            f'## Izlaz\n'
            f'\n'
            f'Opis izlaza\n'
            f'\n'
            f'## Primer\n'
            f'\n'
            f'### Ulaz\n'
            f'\n'
            f'~~~\n'
            f'\n'
            f'~~~\n'
            f'\n'
            f'### Izlaz\n'
            f'\n'
            f'~~~\n'
            f'\n'
            f'~~~\n'
            f'\n'
            , encoding='utf-8'
        )
        if args.verbose:
            print(f'{args.task_name + ".md"} created in {path}')
    except FileExistsError:
        print(f'Error: {path_md} already exists', file=sys.stderr)
    except Exception as e:
        print(f'Error: {path_md}: {e}', file=sys.stderr)

def overall_score(results: List[TestResult]) -> TestResult:
    overall_result = TestResult.OK
    if any(result == TestResult.RTE for result in results):
        overall_result = TestResult.RTE
    if any(result == TestResult.TLE for result in results):
        overall_result = TestResult.TLE
    if any(result == TestResult.WA for result in results) and overall_result == TestResult.OK:
        overall_result = TestResult.WA
    return overall_result

def test_task(args):
    task_dir: Path | None = None

    for path in Path(CURRENT_DIR).iterdir():
        if path.is_dir() and path.name.startswith(args.task_num):
            task_dir = path
            break

    if task_dir is None:
        print(f'Error: task with prefix {args.task_num} not found.', file=sys.stderr)
        return

    problem_meta = get_problem_meta(task_dir)
    if problem_meta is None:
        print(f'Error: could not read problem metadata for task {task_dir.name}.', file=sys.stderr)
        return

    tests = discover_tests(task_dir / TEST_DIR)
    if not tests:
        print(f'Warning: no tests discovered for task {task_dir.name}.')
        return

    compiled_exe = build_exe(problem_meta, task_dir)
    if compiled_exe is None:
        print(f'Error: compilation failed for task {task_dir.name}.', file=sys.stderr)
        return 

    print('Test results:')
    print('-------------')
    answers = []    
    for tcase in tests:
        answers.append(run_test_case(problem_meta, tcase, compiled_exe, verbose=True))
    print('-------------')

    overall_result = overall_score(answers)

    print(f'All result: {overall_result.name} [ '
          f'OK: {answers.count(TestResult.OK)}, ' 
          f'RTE: {answers.count(TestResult.RTE)}, ' 
          f'TLE: {answers.count(TestResult.TLE)}, '
          f'WA: {answers.count(TestResult.WA)} ]'
        )

def run_task(args):
    task_dir: Path | None = None

    for path in Path(CURRENT_DIR).iterdir():
        if path.is_dir() and path.name.startswith(args.task_num):
            task_dir = path
            break

    if task_dir is None:
        print(f'Error: task with prefix {args.task_num} not found.', file=sys.stderr)
        return

    problem_meta: ProblemMeta = get_problem_meta(task_dir)
    if problem_meta is None:
        print(f'Error: could not read problem metadata for task {task_dir.name}.', file=sys.stderr)
        return

    compiled_exe = build_exe(problem_meta, task_dir)
    if compiled_exe is None:
        print(f'Error: compilation failed for task {task_dir.name}.', file=sys.stderr)
        return 

    print(f'Running task {problem_meta.pname}...')

    with tempfile.TemporaryDirectory(prefix="yacpit_run_") as tmpdir:
        work_dir = Path(tmpdir)
        binary_path = work_dir / compiled_exe.name
        shutil.copy2(compiled_exe, binary_path)
        binary_path.chmod(0o755)

        cmd: list[str] = [str(binary_path)]
        run_proc = subprocess.run(
            cmd,
            cwd=work_dir,
            text=True,
        )

def trim_markdown_meta(md_text: str) -> str:
    """Remove YAML front matter from markdown text."""
    lines = md_text.splitlines()
    if len(lines) < 3 or lines[0].strip() != '---':
        return md_text  # No front matter

    end_index = None
    for i in range(1, len(lines)):
        if lines[i].strip() == '---':
            end_index = i
            break

    if end_index is not None:
        return '\n'.join(lines[end_index + 1:]).lstrip()
    else:
        return md_text  # No closing '---', return original

def render_markdown_via_github(md_text: str) -> str:
    """Use GitHub's Markdown API to obtain HTML that matches github.com."""
    api_url = "https://api.github.com/markdown"
    payload = json.dumps({
        "text": md_text,
        "mode": "gfm",
    }).encode("utf-8")

    headers = {
        "Accept": "application/vnd.github+json",
        "Content-Type": "application/json; charset=utf-8",
        "User-Agent": f"yacpit/{VERSION}",
    }

    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    request = urllib.request.Request(api_url, data=payload, headers=headers, method="POST")

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            if response.status != 200:
                raise RuntimeError(f"GitHub Markdown API returned status {response.status}")
            return response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")
        raise RuntimeError(
            f"GitHub Markdown API failed: HTTP {exc.code} {exc.reason}. {detail.strip()}"
        ) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Failed to reach GitHub Markdown API: {exc.reason}") from exc


def render_and_write_html(title: str, md_text: str, out_file: Path) -> bool:
    try:
        body_fragment = render_markdown_via_github(md_text)
    except RuntimeError as exc:
        print(f"Error while contacting GitHub Markdown API: {exc}", file=sys.stderr)
        return False

    full_html = build_github_preview_html(title, body_fragment)

    try:
        out_file.write_text(full_html, encoding="utf-8")
    except OSError as exc:
        print(f"Error writing HTML to {out_file}: {exc}", file=sys.stderr)
        return False

    css_copy = ensure_github_markdown_css(out_file.parent)
    if css_copy is None:
        print("Warning: github-markdown.css was not copied next to the HTML output.", file=sys.stderr)

    tex_js_copy = ensure_tex_mml_chtml_js(out_file.parent)
    if tex_js_copy is None:
        print("Warning: tex-mml-chtml.js was not copied next to the HTML output.", file=sys.stderr)

    print(f'HTML generated at {out_file}')
    return True


def build_github_preview_html(title: str, body_fragment: str) -> str:
    """Wrap GitHub-rendered HTML in the same CSS/layout GitHub preview uses."""
    return f"""<!doctype html>
<html>
  <head>
    <meta charset=\"utf-8\">
    <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
    <title>{title}</title>
    <link rel=\"stylesheet\" href=\"github-markdown.css\">
    <style>
	    .markdown-body {{
	        box-sizing: border-box;
	        min-width: 200px;
	        max-width: 980px;
	        margin: 0 auto;
	        padding: 45px;
	    }}

        @media (max-width: 767px) {{
		.markdown-body {{
			padding: 15px;
		}}
	}}

		.body {{
	   		box-sizing: border-box;
			min-width: 200px;
			max-width: 980px;
			margin: 0 auto;
			padding: 45px;
		}}

		details.solution-section {{
			border: 1px solid #d0d7de;
			border-radius: 6px;
			padding: 1rem;
			background: #f6f8fa;
			margin: 1.5rem 0;
			transition: background-color 0.15s ease, border-color 0.15s ease;
		}}

		details.solution-section summary {{
			cursor: pointer;
			font-weight: 600;
			outline: none;
			color: inherit;
		}}
		details.solution-section[open] {{
			background: #ffffff;
		}}

		@media (prefers-color-scheme: dark) {{
			body {{
				background-color: #0d1117;
				color: #e6edf3;
			}}
			.markdown-body {{
				color: #e6edf3;
			}}
			details.solution-section {{
				border-color: #30363d;
				background: #161b22;
			}}
			details.solution-section[open] {{
				background: #0d1117;
			}}
		}}
    </style>
    <script>
      window.MathJax = {{
        tex: {{
          inlineMath: [['$', '$']],
          displayMath: [['$$', '$$']],
          processEscapes: true,
        }},
        options: {{
          skipHtmlTags: ['script', 'noscript', 'style', 'textarea'],
        }},
      }};
    </script>
    <script id="MathJax-script" async src="tex-mml-chtml.js"></script>
    <script>
      document.addEventListener('DOMContentLoaded', () => {{
        const targets = new Set(['rešenje', 'resenje', 'solution']);
        document.querySelectorAll('.markdown-body h2').forEach((heading) => {{
          const headingText = (heading.textContent || '').trim();
          if (!headingText) {{
            return;
          }}

          const normalized = headingText.toLowerCase();
          if (!targets.has(normalized)) {{
            return;
          }}

          const details = document.createElement('details');
          details.classList.add('solution-section');
          if (heading.id) {{
            details.id = heading.id;
          }}

          const summary = document.createElement('summary');
          summary.textContent = headingText;
          details.appendChild(summary);

          let node = heading.nextSibling;
          while (node) {{
            if (node.nodeType === Node.ELEMENT_NODE && node.tagName === 'H2') {{
              break;
            }}
            const next = node.nextSibling;
            details.appendChild(node);
            node = next;
          }}

          heading.replaceWith(details);
        }});
      }});
    </script>
  </head>
  <body>
    <article class=\"markdown-body\">
{body_fragment}
    </article>
  </body>
</html>
"""


def ensure_github_markdown_css(target_dir: Path) -> Path | None:
    """Copy the packaged github-markdown.css next to the generated HTML."""
    if not GITHUB_MARKDOWN_CSS.exists():
        print(
            f"Error: bundled CSS missing at {GITHUB_MARKDOWN_CSS}.",
            file=sys.stderr,
        )
        return None

    try:
        css_bytes = GITHUB_MARKDOWN_CSS.read_bytes()
    except OSError as exc:
        print(f"Error: failed to read {GITHUB_MARKDOWN_CSS}: {exc}", file=sys.stderr)
        return None

    dest = target_dir / GITHUB_MARKDOWN_CSS.name
    if dest.exists():
        try:
            if dest.read_bytes() == css_bytes:
                return dest
        except OSError:
            pass  # fall back to overwriting

    try:
        dest.write_bytes(css_bytes)
    except OSError as exc:
        print(f"Error: failed to write {dest}: {exc}", file=sys.stderr)
        return None

    return dest

def ensure_tex_mml_chtml_js(target_dir: Path) -> Path | None:
    """Copy the packaged tex-mml-chtml.js next to the generated HTML."""
    if not TEX_MML_CHTML_JS.exists():
        print(
            f"Error: bundled JS missing at {TEX_MML_CHTML_JS}.",
            file=sys.stderr,
        )
        return None

    try:
        js_bytes = TEX_MML_CHTML_JS.read_bytes()
    except OSError as exc:
        print(f"Error: failed to read {TEX_MML_CHTML_JS}: {exc}", file=sys.stderr)
        return None

    dest = target_dir / TEX_MML_CHTML_JS.name
    if dest.exists():
        try:
            if dest.read_bytes() == js_bytes:
                return dest
        except OSError:
            pass  # fall back to overwriting

    try:
        dest.write_bytes(js_bytes)
    except OSError as exc:
        print(f"Error: failed to write {dest}: {exc}", file=sys.stderr)
        return None

    return dest

def get_github_markdown_css_text() -> str:
    try:
        return GITHUB_MARKDOWN_CSS.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"Error: failed to read {GITHUB_MARKDOWN_CSS}: {exc}", file=sys.stderr)
        return ""

def get_tex_mml_chtml_js_text() -> str:
    try:
        return TEX_MML_CHTML_JS.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"Error: failed to read {TEX_MML_CHTML_JS}: {exc}", file=sys.stderr)
        return ""

def get_tester_py_text() -> str:
    try:
        return TESTER_PY.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"Error: failed to read {TESTER_PY}: {exc}", file=sys.stderr)
        return ""

def append_solution_section(md_text: str, problem_meta: ProblemMeta, task_dir: Path) -> str:
    solution_section = "\n\n## Rešenje\n\n"
    for code in problem_meta.headers + problem_meta.sources:
        if code.exists():
            code_text = code.read_text(encoding="utf-8")
            solution_section += f"### `{code.name}`\n\n"
            solution_section += f"```{problem_meta.language}\n{code_text}\n```\n\n"
    # include Makefile if present (either 'Makefile' or 'makefile')
    for mname in ("Makefile", "makefile"):
        mpath = task_dir / mname
        if mpath.exists() and mpath.is_file():
            try:
                mtext = mpath.read_text(encoding="utf-8")
            except OSError:
                mtext = ""
            solution_section += f"### `{mpath.name}`\n\n"
            solution_section += f"```makefile\n{mtext}\n```\n\n"
    return md_text + solution_section


def generate_html_for_task(task_dir: Path, output_dir: Path):
    problem_meta: ProblemMeta = get_problem_meta(task_dir)
    if problem_meta is None:
        print(f'Error: could not read problem metadata for task {task_dir.name}.', file=sys.stderr)
        return

    md_file: Path | None = None
    for path in task_dir.iterdir():
        if path.is_file() and path.suffix == ".md":
            md_file = path
            break

    if md_file is None:
        print(f"Markdown file (.md) not found in {task_dir}.", file=sys.stderr)
        return 

    html_file = output_dir / (md_file.stem + ".html")

    md_text = md_file.read_text(encoding="utf-8")
    md_text = trim_markdown_meta(md_text)
    md_text = append_solution_section(md_text, problem_meta, task_dir)
    title = problem_meta.pname or md_file.stem
    success = render_and_write_html(title, md_text, html_file)
    if not success:
        return

def html_task(args):
    task_dir: Path | None = None

    for path in Path(CURRENT_DIR).iterdir():
        if path.is_dir() and path.name.startswith(args.task_num):
            task_dir = path
            break

    if task_dir is None:
        print(f'Error: task with prefix {args.task_num} not found.', file=sys.stderr)
        return

    output_dir = Path(args.output)
    if not output_dir.is_dir():
        output_dir.mkdir(parents=True, exist_ok=True)
        return

    generate_html_for_task(task_dir, Path(args.output))

def get_github_markdown_css(args):
    css_text = get_github_markdown_css_text()
    output_file = Path(CURRENT_DIR) / "github-markdown.css"
    try:
        output_file.write_text(css_text, encoding="utf-8")
        print(f'GitHub markdown CSS written to {output_file}')
    except OSError as exc:
        print(f'Error writing to {output_file}: {exc}', file=sys.stderr)

def get_tex_mml_chtml_js(args):
    js_text = get_tex_mml_chtml_js_text()
    output_file = Path(CURRENT_DIR) / "tex-mml-chtml.js"
    try:
        output_file.write_text(js_text, encoding="utf-8")
        print(f'TeX to MathML to CHTML JS written to {output_file}')
    except OSError as exc:
        print(f'Error writing to {output_file}: {exc}', file=sys.stderr)

def get_tester(args):
    tester_text = get_tester_py_text()
    tester_path = Path(CURRENT_DIR) / "tester.py"
    try:
        tester_path.write_text(tester_text, encoding="utf-8")
        print(f'Tester script written to {tester_path}')
    except OSError as exc:
        print(f'Error writing to {tester_path}: {exc}', file=sys.stderr)

def remove_tester(args):
    """Remove tester.py from the solutions directory and all its subdirectories."""
    solutions_dif = Path(CURRENT_DIR) / "solutions"
    if not solutions_dif.exists() or not solutions_dif.is_dir():
        print(f'Error: solutions directory not found at {solutions_dif}', file=sys.stderr)
        return
    for path in solutions_dif.rglob("tester.py"):
        try:
            path.unlink()
            print(f'Removed {path}')
        except OSError as exc:
            print(f'Error removing {path}: {exc}', file=sys.stderr)
    
def html_all(args):
    task_dirs = [p for p in Path(CURRENT_DIR).iterdir() if p.is_dir() and re.match(r"^\d+_", p.name)]
    if not task_dirs:
        print('No tasks found to generate HTML for.')
        return

    output_dir = Path(args.output)
    if not output_dir.is_dir():
        output_dir.mkdir(parents=True, exist_ok=True)

    for task_dir in sorted(task_dirs, key=lambda p: p.name):
        print(f'Generating HTML for {task_dir.name}...')
        generate_html_for_task(task_dir, Path(args.output))

def make_exam(args):
    title: str = args.exam_name
    task_nums: List[str] = args.task_nums
    task_dirs: List[Path] = []

    output_dir: Path = Path(args.output)
    if not output_dir.is_dir():
        output_dir.mkdir(parents=True, exist_ok=True)

    for tnum in task_nums:
        found = False
        for path in Path(CURRENT_DIR).iterdir():
            if path.is_dir() and path.name.startswith(tnum):
                task_dirs.append(path)
                found = True
                break
        if not found:
            print(f'Error: task with prefix {tnum} not found.', file=sys.stderr)
            return

    exam_md: str = f"# {title}\n\n"
    exam_md += f"*Pravila igre:*\n\n"
    exam_md += f"Pre početka preimenovati direktorijum tako da sadrži Vaše ime, prezime i indeks (u alas formatu). "
    exam_md += f"Bitno je da NE promenite prefiks u nazivu direktorijuma kao i strukturu direktorijuma.\n\n"
    exam_md += f"Na raspolaganju imate tester skriptu `tester.py` koji se nalazi u Vašem direktorijumu. "
    exam_md += f"Tester se pokreće iz komandne linije komandom `./tester.py` i on će automatski testirati sve zadatke i test primere koje ste dobili. "
    exam_md += f"Tester se može pokrenuti i sa argumentom koji predstavlja prefiks zadatka, npr. `./tester.py 01` će pokrenuti samo testove za prvi zadatak. "
    exam_md += f"Tester se može pokrenuti i sa argumentom `-v` koji će omogućiti detaljniji ispis rezultata testiranja.\n\n"
    exam_md += f"*Napomene*: Tester je dat kao pomoćni alat i nije sveobuhvatan. "
    exam_md += f"Pokriva osnovne test primere, ali nije garancija da će uhvatiti sve moguće greške u rešenju. "
    exam_md += f"Od Vas se očekuje da temeljno testirate svoja rešenja i da se ne oslanjate isključivo na tester. "
    exam_md += f"Tester ne ocenjuje Vaša rešenja, već se ona ocenjuju ručno, zbog toga je važno da ona budu korektna i u odgovarajućoj vremenskoj i prostornoj složenosti.\n\n"
    exam_md += f"\n\n---\n\n"

    for task_dir in task_dirs:
        md_file: Path | None = None
        for path in task_dir.iterdir():
            if path.is_file() and path.suffix == ".md":
                md_file = path
                break

        if md_file is None:
            print(f"Markdown file (.md) not found in {task_dir}.", file=sys.stderr)
            return 

        md_text = md_file.read_text(encoding="utf-8")
        md_text = trim_markdown_meta(md_text)
        exam_md += md_text + "\n\n---\n\n" 

    exam_html = output_dir / (title.replace(" ", "_") + ".html")
    success = render_and_write_html(title, exam_md, exam_html)
    if not success:
        return

    tests_output_dir = output_dir / "tests"
    if not tests_output_dir.exists():
        tests_output_dir.mkdir(parents=True, exist_ok=True)

    for task_dir in task_dirs:
        task_tests_dir = task_dir / TEST_DIR
        if not task_tests_dir.exists():
            continue
        dest_task_tests_dir = tests_output_dir / task_dir.name
        if not dest_task_tests_dir.exists():
            dest_task_tests_dir.mkdir(parents=True, exist_ok=True)
        for test_file in task_tests_dir.iterdir():
            if test_file.is_file():
                shutil.copy2(test_file, dest_task_tests_dir / test_file.name)    
    print(f'Tests copied to {tests_output_dir}!')
    
    solutions_dir = output_dir / (DEFAULT_EXAM_TEMPLATE_COURSE + DEFAULT_EXAM_TEMPLATE_STUDENT)
    if not solutions_dir.exists():
        solutions_dir.mkdir(parents=True, exist_ok=True)

    for task_dir in task_dirs:
        student_task_dir = solutions_dir / task_dir.name
        if not student_task_dir.exists():
            student_task_dir.mkdir(parents=True, exist_ok=True)
        problem_meta: ProblemMeta = get_problem_meta(task_dir)
        if problem_meta is None:
            continue
        for src in problem_meta.sources:
            student_src = student_task_dir / src.name
            if not student_src.exists():
                student_src.touch()
    print(f'Solution templates created in {solutions_dir}!')

    statements_dir = output_dir / "statements"
    if not statements_dir.exists():
        statements_dir.mkdir(parents=True, exist_ok=True)
    
    for task_dir in task_dirs:
        statement_task_dir = statements_dir / task_dir.name
        if not statement_task_dir.exists():
            statement_task_dir.mkdir(parents=True, exist_ok=True)
        md_file: Path | None = None
        for path in task_dir.iterdir():
            if path.is_file() and path.suffix == ".md":
                md_file = path
                break
        if md_file is None:
            print(f"Markdown file (.md) not found in {task_dir}.", file=sys.stderr)
            return 
        shutil.copy2(md_file, statement_task_dir / md_file.name)
    print(f'Statements copied to {statements_dir}!')

def grade_student_solution(student_dir: Path, tests_dir: Path, statements_dir: Path, verbose: bool) -> dict[str, int]:
    student_name = student_dir.name[len(DEFAULT_EXAM_TEMPLATE_COURSE):]
    total_score = 0
    scores: dict[str, int] = {}

    for statement_dir in sorted(statements_dir.iterdir(), key=lambda p: p.name):
        for student_task_dir in student_dir.rglob("*"):
            if not student_task_dir.is_dir() or not re.match(r"^\d+_", student_task_dir.name):
                continue
            if student_task_dir.name == statement_dir.name:
                break

        scores[statement_dir.name] = 0

        problem_meta: ProblemMeta = get_problem_meta(statements_dir / student_task_dir.name, student_task_dir)
        if problem_meta is None:
            print(f'Error: could not read problem metadata for task {student_task_dir.name}.', file=sys.stderr)
            continue

        tests = discover_tests(tests_dir / student_task_dir.name)
        if not tests:
            print(f'Warning: no tests discovered for task {student_task_dir.name}.')
            continue

        compiled_exe = build_exe(problem_meta, student_task_dir)
        if compiled_exe is None:
            print(f'Error: compilation failed for student {student_name}, task {student_task_dir.name}.', file=sys.stderr)
            continue 

        if verbose:
            print(f'Grading student {student_name}, task {student_task_dir.name}...')
        answers = []
        for tcase in tests:
            answers.append(run_test_case(problem_meta, tcase, compiled_exe, verbose=verbose))

        overall_result = overall_score(answers)

        print(f'Student: {student_name}, Task: {student_task_dir.name}, Result: {overall_result.name} [ '
              f'OK: {answers.count(TestResult.OK)}, '
              f'RTE: {answers.count(TestResult.RTE)}, '
              f'TLE: {answers.count(TestResult.TLE)}, '
              f'WA: {answers.count(TestResult.WA)} ]'
             )

        score = int((answers.count(TestResult.OK) / len(answers)) * 100)
        scores[student_task_dir.name] = score
        total_score += score

    scores["total_score"] = total_score
    return scores

def grade_student(args):
    exam_dir: Path = Path(args.path)
    verbose: bool = args.verbose

    if not exam_dir.exists():
        print(f'Error: exam directory {exam_dir} does not exist.', file=sys.stderr)
        return

    tests_dir = exam_dir / TEST_DIR
    if not tests_dir.exists():
        print(f'Error: tests directory {tests_dir} does not exist.', file=sys.stderr)
        return

    statements_dir = exam_dir / "statements"
    if not statements_dir.exists():
        print(f'Error: statements directory {statements_dir} does not exist.', file=sys.stderr)
        return

    solutions_dir = exam_dir / "solutions"
    if not solutions_dir.exists():
        print(f'Error: solutions directory {solutions_dir} does not exist.', file=sys.stderr)
        return

    student_dir = None
    for path in solutions_dir.iterdir():
        if path.is_dir() and args.student in path.name:
            student_dir = path
            break
    
    if student_dir is None:
        print(f'Error: student solution for {args.student} not found in {solutions_dir}.', file=sys.stderr)
        return

    grades: dict[str, int] = grade_student_solution(
        student_dir,
        tests_dir,
        statements_dir,
        verbose=True
    )

    print(f'Grades for student {student_dir.name[len(DEFAULT_EXAM_TEMPLATE_COURSE):]}:')
    for task in grades:
        if task != "total_score":
            print(f'  {task}: {grades[task]}')
    print(f'  Total score: {grades["total_score"]}')


def grade_exam(args):
    exam_path: Path = Path(args.path)
    output_dir: Path = Path(args.output)
    verbose: bool = args.verbose

    tests_dir = exam_path / TEST_DIR
    if not tests_dir.exists():
        print(f'Error: tests directory {tests_dir} does not exist.', file=sys.stderr)
        return

    solutions_dir = exam_path / "solutions"
    if not solutions_dir.exists():
        print(f'Error: solutions directory {solutions_dir} does not exist.', file=sys.stderr)
        return

    if not output_dir.is_dir():
        output_dir.mkdir(parents=True, exist_ok=True)

    statements_dir = exam_path / "statements"
    if not statements_dir.exists():
        print(f'Error: statements directory {statements_dir} does not exist.', file=sys.stderr)
        return

    number_of_statements = sum(1 for p in statements_dir.iterdir() if p.is_dir() and re.match(r"^\d+_", p.name))
    if number_of_statements == 0:
        print(f'Error: no statements found in {statements_dir}.', file=sys.stderr)
        return

    csv_file = output_dir / "grades.csv"
    with csv_file.open("w", encoding="utf-8") as cf:
        cf.write("student_id,total_score")
        for statement_dir in sorted(statements_dir.iterdir(), key=lambda p: p.name):
            if statement_dir.is_dir() and re.match(r"^\d+_", statement_dir.name):
                cf.write(f",{statement_dir.name}")
        cf.write("\n")
    
        for solution_subdir in solutions_dir.iterdir():
            if not solution_subdir.is_dir() or not solution_subdir.name.startswith(DEFAULT_EXAM_TEMPLATE_COURSE):
                continue

            student_name = solution_subdir.name[len(DEFAULT_EXAM_TEMPLATE_COURSE):]
            student_id = student_name  # default to full name if no match

            # NameSurname_mXYYNNN -> NNN/20YY (X is one of i,l,m,p,r,s or uppercase)
            sufix_match = re.search(r'_m[ilmprsILMPRS](\d{2})(\d{3})$', student_name)
            if sufix_match:
                year = sufix_match.group(1)
                index = sufix_match.group(2).lstrip('0')
                student_id = f"{index}/20{year}"

            cf.write(f"{student_id}")

            grades: dict[str, int] = grade_student_solution(
                solution_subdir,
                tests_dir,
                statements_dir,
                verbose=verbose
            )

            cf.write(f",{grades['total_score']}")
            for task in grades:
                if task != "total_score":
                    cf.write(f",{grades[task]}")
            cf.write("\n")
   

def unzip_student_solution(student_zip: Path, output_dir: Path):
    student_name = student_zip.stem

    try:
        with zipfile.ZipFile(student_zip, 'r') as zf:
            zf.extractall(output_dir)
        print(f'Extracted {student_zip} to {output_dir}')
    except zipfile.BadZipFile:
        print(f'Error: {student_zip} is not a valid zip file.', file=sys.stderr)
    except Exception as e:
        print(f'Error extracting {student_zip}: {e}', file=sys.stderr)

def unzip_solutions(args):
    zip_file: Path = Path(args.zip_file)
    output_dir: Path = Path(args.output)

    if not zip_file.exists():
        print(f'Error: zip file {zip_file} does not exist.', file=sys.stderr)
        return

    if not output_dir.is_dir():
        output_dir.mkdir(parents=True, exist_ok=True)

    try:
        with zipfile.ZipFile(zip_file, 'r') as zf:
            zf.extractall(output_dir)
    except zipfile.BadZipFile:
        print(f'Error: {zip_file} is not a valid zip file.', file=sys.stderr)
    except Exception as e:
        print(f'Error extracting {zip_file}: {e}', file=sys.stderr)

    solutions_dir = output_dir / "solutions"
    if not solutions_dir.exists():
        print(f'Error: solutions directory {solutions_dir} does not exist after extraction.', file=sys.stderr)
        return
    
    for student_zip in solutions_dir.iterdir():
        if student_zip.is_file() and student_zip.suffix == ".zip":
            unzip_student_solution(student_zip, solutions_dir)
    
    for student_zip in solutions_dir.iterdir():
        if student_zip.is_file() and student_zip.suffix == ".zip":
            try:
                student_zip.unlink()
            except Exception as e:
                print(f'Error deleting {student_zip}: {e}', file=sys.stderr)

    

def build_parser() -> argparse.ArgumentParser:

    p = argparse.ArgumentParser(prog="yacpit", description="Yet Another Competitive Programming Interface Tool")

    p.add_argument("-v", "--verbose", action="store_true", help="increase output verbosity")
    p.add_argument("--version", action="version", version=VERSION)

    sub_p = p.add_subparsers(dest="cmd")

    init_env_p = sub_p.add_parser("init-env", help="Initiate new environment")
    init_env_p.add_argument("-p", "--path", type=str, default=DEFAULT_ENV_PATH, help="path for new environment")
    init_env_p.set_defaults(func=init_env)

    init_task_p = sub_p.add_parser("init-task", help="Initiate new task")
    init_task_p.add_argument("task_num", type=str, help="task number")
    init_task_p.add_argument("task_name", type=str, help="task name")
    init_task_p.add_argument("-p", "--path", type=str, default=CURRENT_DIR, help="environment path")
    init_task_p.set_defaults(func=init_task)

    test_task_p = sub_p.add_parser("test-task", help="Test the given solution for a given task")
    test_task_p.add_argument("task_num", type=str, help="task number")
    test_task_p.set_defaults(func=test_task)

    run_task_p = sub_p.add_parser("run-task", help="Run the given solution for a given task")
    run_task_p.add_argument("task_num", type=str, help="task number")
    run_task_p.set_defaults(func=run_task)

    html_task_p = sub_p.add_parser("html-task", help="Generate HTML for the given task")
    html_task_p.add_argument("task_num", type=str, help="task number")
    html_task_p.add_argument("-o", "--output", type=str, default=CURRENT_DIR, help="output directory")
    html_task_p.set_defaults(func=html_task)

    html_all_p = sub_p.add_parser("html-all", help="Generate HTML for all tasks")
    html_all_p.add_argument("-o", "--output", type=str, default=CURRENT_DIR, help="output directory")
    html_all_p.set_defaults(func=html_all)

    get_css_p = sub_p.add_parser("get-github-css", help="Get GitHub markdown CSS")
    get_css_p.set_defaults(func=get_github_markdown_css)

    get_tex_p = sub_p.add_parser("get-tex-js", help="Get MathJax TeX-ML-CHTML JS")
    get_tex_p.set_defaults(func=get_tex_mml_chtml_js)
    
    get_tester_p = sub_p.add_parser("get-tester", help="Get exam tester for students")
    get_tester_p.set_defaults(func=get_tester)

    remove_tester_p = sub_p.add_parser("remove-tester", help="Remove all testers from solutions directory")
    remove_tester_p.set_defaults(func=remove_tester)

    make_exam_p = sub_p.add_parser("make-exam", help="Generate exam template for the given tasks")
    make_exam_p.add_argument("exam_name", type=str, help="exam name")
    make_exam_p.add_argument("task_nums", type=str, nargs="+", help="list of task numbers")
    make_exam_p.add_argument("-o", "--output", type=str, default=CURRENT_DIR, help="output directory")
    make_exam_p.set_defaults(func=make_exam)

    grade_exam_p = sub_p.add_parser("grade-exam", help="Grade exam solutions")
    grade_exam_p.add_argument("-p", "--path", type=str, default=CURRENT_DIR, help="path to the exam")
    grade_exam_p.add_argument("-o", "--output", type=str, default=CURRENT_DIR, help="output directory")
    grade_exam_p.add_argument("-v", "--verbose", action="store_true", help="increase output verbosity")
    grade_exam_p.set_defaults(func=grade_exam)

    grade_student_p = sub_p.add_parser("grade-student", help="Grade a single student's exam solution")
    grade_student_p.add_argument("student", type=str, help="Student name or identifier")
    grade_student_p.add_argument("-p", "--path", type=str, default=CURRENT_DIR, help="path to the exam")
    grade_student_p.add_argument("-v", "--verbose", action="store_true", help="increase output verbosity")
    grade_student_p.set_defaults(func=grade_student)

    unzip_solutions_p = sub_p.add_parser("unzip-solutions", help="Unzip submitted exam solutions")
    unzip_solutions_p.add_argument("zip_file", type=str, help="path to the zip file")
    unzip_solutions_p.add_argument("-o", "--output", type=str, default=CURRENT_DIR, help="output directory")
    unzip_solutions_p.set_defaults(func=unzip_solutions)

    return p

def main(argv: list[str] | None = None) -> int:

    args_parser = build_parser()
    args = args_parser.parse_args(argv)

    if args.cmd is None:
        return 1
    else:
        args.func(args)

    return 0

if __name__ == "__main__":
    raise SystemExit(main())
