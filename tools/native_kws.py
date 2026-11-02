"""Verify pinned KWS source fixes; never loads or replaces a runtime library."""

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
RESOURCE = ROOT / 'tools/resources/sherpa-kws'
SNAPSHOT = RESOURCE / 'upstream'
COMMIT = 'af2d8d81dbfa61c4cea4c568da78f3982edca005'
DECODER = 'sherpa-onnx/csrc/transducer-keyword-decoder.cc'
SPOTTER = 'sherpa-onnx/csrc/keyword-spotter-transducer-impl.h'
FILES = frozenset(
    ('LICENSE', DECODER, SPOTTER)
    + tuple(
        'sherpa-onnx/csrc/' + name
        for name in ('context-graph.h', 'context-graph.cc', 'log.h', 'macros.h')
    )
)
PATCHES = {
    DECODER: (
        'ys_prob += best_hyp.ys_probs[i];',
        'ys_prob += best_hyp.ys_probs[best_hyp.ys_probs.size() -\n'
        '                                         matched_state->level + i];',
    ),
    SPOTTER: (
        'if (trailing_silence > threshold) {\n        Reset(s);\n      }',
        'if (trailing_silence > threshold) {\n        ResetKeywordResult(s);\n      }',
    ),
}
RESET_BEFORE = """  void InitOnlineStream(OnlineStream *stream) const {
    auto r = decoder_->GetEmptyResult();
    SHERPA_ONNX_CHECK_EQ(r.hyps.Size(), 1);

    SHERPA_ONNX_CHECK(stream->GetContextGraph() != nullptr);
    r.hyps.begin()->second.context_state = stream->GetContextGraph()->Root();

    stream->SetKeywordResult(r);
    stream->SetStates(model_->GetEncoderInitStates());
  }"""
RESET_AFTER = """  void InitOnlineStream(OnlineStream *stream) const {
    ResetKeywordResult(stream);
    stream->SetStates(model_->GetEncoderInitStates());
  }

  void ResetKeywordResult(OnlineStream *stream) const {
    auto r = decoder_->GetEmptyResult();
    SHERPA_ONNX_CHECK_EQ(r.hyps.Size(), 1);

    SHERPA_ONNX_CHECK(stream->GetContextGraph() != nullptr);
    r.hyps.begin()->second.context_state = stream->GetContextGraph()->Root();

    stream->SetKeywordResult(r);
  }"""


MODIFICATION_NOTICES = {
    DECODER: '// Modified by CC8: confidence uses the matched probability suffix.\n',
    SPOTTER: '// Modified by CC8: automatic reset preserves encoder; explicit reset is unchanged.\n',
}


def verified_sources(root=SNAPSHOT):
    manifest = json.loads((RESOURCE / 'upstream.json').read_text())
    if manifest.get('commit') != COMMIT or set(manifest.get('files', {})) != FILES:
        raise ValueError('Unexpected native source manifest')
    result = {}
    for name in sorted(FILES):
        path = root / name
        try:
            for parent in path.relative_to(root).parents:
                if (root / parent).is_symlink():
                    raise ValueError('Linked native source directory')
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, 'rb') as source:
                info = os.fstat(source.fileno())
                expected = manifest['files'][name]
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_nlink != 1
                    or not 0 < info.st_size <= 100000
                    or info.st_size != expected['bytes']
                ):
                    raise ValueError('Unsafe or incomplete native source')
                data = source.read(100001)
                if len(data) != info.st_size:
                    raise ValueError('Native source changed while reading')
        except OSError:
            raise ValueError('Missing native source') from None
        if hashlib.sha256(data).hexdigest() != expected['sha256']:
            raise ValueError('Native source checksum mismatch')
        result[name] = data.decode('utf-8')
    return result


def patched_sources(root=SNAPSHOT):
    sources = verified_sources(root)
    for name, (before, after) in PATCHES.items():
        if sources[name].count(before) != 1:
            raise ValueError('Unexpected native patch context')
        sources[name] = sources[name].replace(before, after)
    if sources[SPOTTER].count(RESET_BEFORE) != 1:
        raise ValueError('Unexpected native reset helper context')
    sources[SPOTTER] = sources[SPOTTER].replace(RESET_BEFORE, RESET_AFTER)
    # Explicit lifecycle resets must still clear the encoder, unlike the
    # automatic silence branch. This is a source invariant, not a binary test.
    explicit = 'void Reset(OnlineStream *s) const override { InitOnlineStream(s); }'
    if sources[SPOTTER].count(explicit) != 1:
        raise ValueError('Explicit keyword reset changed')
    expected = json.loads((RESOURCE / 'upstream.json').read_text())['evaluated_patched_body_sha256']
    if set(expected) != set(PATCHES) or any(
        hashlib.sha256(sources[name].encode()).hexdigest() != value
        for name, value in expected.items()
    ):
        raise ValueError('Patched source differs from evaluated native build')
    # Apache-2.0 modification notices are added after comparing evaluated
    # source bodies. Exported full-file digests also include these comments.
    for name, notice in MODIFICATION_NOTICES.items():
        sources[name] = notice + sources[name]
    return sources


def member(code, signature):
    begin = code.index(signature)
    brace = code.index('{', begin)
    depth = 1
    end = brace + 1
    while depth and end < len(code):
        depth += (code[end] == '{') - (code[end] == '}')
        end += 1
    if depth:
        raise ValueError('Incomplete fixed native member')
    return code[begin:end]


def compile_fixture(sources, root, compiler, kind):
    for name, text in sources.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    template = (RESOURCE / (kind + '-repro.cc.in')).read_text()
    if kind == 'confidence':
        code = sources[DECODER]
        begin = code.index('float ys_prob = 0.0;')
        end = code.index('if (best_hyp.num_trailing_blanks >', begin)
        template = template.replace('/* CC8_NATIVE_CONFIDENCE_EXPRESSION */', code[begin:end])
    elif kind == 'reset':
        code = sources[SPOTTER]
        members = member(code, '  void Reset(OnlineStream *s) const override')
        members += '\n' + member(code, '  void InitOnlineStream(OnlineStream *stream) const')
        if '  void ResetKeywordResult(OnlineStream *stream) const' in code:
            members += '\n' + member(code, '  void ResetKeywordResult(OnlineStream *stream) const')
        automatic = member(code, '      if (trailing_silence > threshold)')
        template = template.replace('/* CC8_NATIVE_RESET_MEMBERS */', members)
        template = template.replace('/* CC8_NATIVE_AUTOMATIC_RESET */', automatic)
    else:
        raise ValueError('Unknown native regression fixture')
    program = root / 'repro.cc'
    program.write_text(template)
    binary = root / 'repro'
    subprocess.run(
        [
            compiler,
            '-std=c++17',
            '-O2',
            '-I',
            str(root),
            str(program),
            str(root / 'sherpa-onnx/csrc/context-graph.cc'),
            '-o',
            str(binary),
        ],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=True,
        timeout=30,
    )
    completed = subprocess.run(
        [str(binary)],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=True,
        timeout=5,
    )
    return json.loads(completed.stdout)


def verify(root=SNAPSHOT):
    original, corrected = verified_sources(root), patched_sources(root)
    compiler = shutil.which('c++')
    if compiler is None:
        raise RuntimeError('A C++17 compiler is required')
    with tempfile.TemporaryDirectory(prefix='cc8-native-source-') as temporary:
        folder = Path(temporary)
        before = compile_fixture(original, folder / 'before', compiler, 'confidence')
        after = compile_fixture(corrected, folder / 'after', compiler, 'confidence')
        reset_before = compile_fixture(original, folder / 'reset-before', compiler, 'reset')
        reset_after = compile_fixture(corrected, folder / 'reset-after', compiler, 'reset')
    expected_before = {'high_history': 1.99 / 3, 'low_history': 0.32 / 3, 'no_backoff_control': 0.4}
    expected_after = {'high_history': 0.01, 'low_history': 0.3, 'no_backoff_control': 0.4}
    for observed, expected in ((before, expected_before), (after, expected_after)):
        if set(observed) != set(expected) or any(
            not math.isclose(observed[name], value, abs_tol=1e-5)
            for name, value in expected.items()
        ):
            raise RuntimeError('Native confidence regression did not match')
    reset_expected = {
        'automatic_encoder_preserved': False,
        'explicit_encoder_reset': True,
        'keyword_result_reset': True,
    }
    if reset_before != reset_expected or reset_after != {
        **reset_expected,
        'automatic_encoder_preserved': True,
    }:
        raise RuntimeError('Native reset regression did not match')
    return {
        'state': 'passed',
        'scope': 'pinned_native_source_regression',
        'upstream_commit': COMMIT,
        'before': before,
        'after': after,
        'reset_before': reset_before,
        'reset_after': reset_after,
        'explicit_reset_preserved': True,
        'patched_source_sha256': {
            name: hashlib.sha256(corrected[name].encode()).hexdigest() for name in PATCHES
        },
        'evaluated_patched_body_sha256': json.loads((RESOURCE / 'upstream.json').read_text())[
            'evaluated_patched_body_sha256'
        ],
        'installed_native_tested': False,
        'runtime_changed': False,
        'microphone_used': False,
        'network_requested': False,
        'full_p0_acceptance': False,
    }


def write_overlay(destination, root=SNAPSHOT):
    sources = patched_sources(root)
    destination.mkdir(mode=0o700, exist_ok=False)
    # An existing destination is never reused. A failed new overlay remains
    # incomplete without its final manifest; no caller should build from it.
    rows = {}
    for name in sorted(set(PATCHES) | {'LICENSE'}):
        path = destination / name
        parent = destination
        for part in Path(name).parts[:-1]:
            parent /= part
            parent.mkdir(mode=0o700, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w') as output:
            output.write(sources[name])
        rows[name] = hashlib.sha256(sources[name].encode()).hexdigest()
    fd = os.open(destination / 'manifest.json', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as output:
        json.dump(
            {
                'state': 'complete',
                'upstream_commit': COMMIT,
                'files': rows,
                'contains_runtime_library': False,
            },
            output,
            indent=2,
        )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=SNAPSHOT)
    parser.add_argument(
        '--overlay', type=Path, help='New directory for verified source changes only'
    )
    args = parser.parse_args(argv)
    try:
        result = verify(args.source)
        if args.overlay is not None:
            write_overlay(args.overlay, args.source)
        print(json.dumps(result))
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
        print(
            'Native source check failed; verify the pinned snapshot and C++17 compiler.',
            file=sys.stderr,
        )
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
