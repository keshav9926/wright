"""L2 tests — TS extractor: exports, arrow consts, methods, interfaces."""

from __future__ import annotations

from wright_index.extract.typescript import extract
from wright_index.parsers import get_parser

SAMPLE = '''\
/** Options for the client. */
export interface ClientOptions {
  url: string;
}

export type Handler = (req: string) => Promise<void>;

/**
 * Fetches a thing.
 */
export async function fetchThing(url: string): Promise<string> {
  return url;
}

function localHelper(): void {}

export const handler = async (req: string) => {
  return req;
};

const localArrow = (x: number) => x * 2;

export class Client {
  /** Connects the client. */
  connect(opts: ClientOptions): void {}

  private teardown(): void {}
}
'''


def _by_qname(source: str, lang: str = "typescript"):
    data = source.encode()
    tree = get_parser(lang).parse(data)
    return {s.qualified_name: s for s in extract(data, tree)}


def test_finds_all_definitions():
    syms = _by_qname(SAMPLE)
    assert set(syms) == {
        "ClientOptions", "Handler", "fetchThing", "localHelper",
        "handler", "localArrow", "Client", "Client.connect", "Client.teardown",
    }


def test_kinds():
    syms = _by_qname(SAMPLE)
    assert syms["ClientOptions"].kind == "interface"
    assert syms["Handler"].kind == "type_alias"
    assert syms["fetchThing"].kind == "function"
    assert syms["handler"].kind == "function"          # const-arrow idiom
    assert syms["Client"].kind == "class"
    assert syms["Client.connect"].kind == "method"


def test_export_detection():
    syms = _by_qname(SAMPLE)
    assert syms["fetchThing"].is_exported
    assert syms["handler"].is_exported                 # export const ... = () =>
    assert syms["Client"].is_exported
    assert not syms["localHelper"].is_exported
    assert not syms["localArrow"].is_exported
    # methods: visibility from accessibility modifier, not export keyword
    assert syms["Client.connect"].is_exported
    assert not syms["Client.teardown"].is_exported     # private


def test_docstrings_from_jsdoc():
    syms = _by_qname(SAMPLE)
    assert syms["ClientOptions"].docstring == "Options for the client."
    assert syms["fetchThing"].docstring == "Fetches a thing."   # above export keyword
    assert syms["Client.connect"].docstring == "Connects the client."


def test_arrow_const_signature_starts_at_const():
    syms = _by_qname(SAMPLE)
    assert syms["handler"].signature.startswith("const handler = async (req: string)")


def test_tsx_grammar_same_extractor(tmp_path):
    """The tsx grammar parses JSX; the same extractor must work unchanged."""
    src = '''\
export const App = () => {
  return <div>hello</div>;
};

export function Page(): JSX.Element {
  return <App />;
}
'''
    data = src.encode()
    tree = get_parser("tsx").parse(data)
    syms = {s.qualified_name: s for s in extract(data, tree, lang_key="tsx")}
    assert set(syms) == {"App", "Page"}
    assert syms["App"].kind == "function"
    assert syms["App"].is_exported
