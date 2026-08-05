"""L2 tests — Go extractor: receivers, type classification, doc comments."""

from __future__ import annotations

from wright_index.extract.go import extract

SAMPLE = '''\
package device

// Device represents one accelerator card.
// It is safe for concurrent use.
type Device struct {
	ID   string
	used bool
}

// Runner runs things.
type Runner interface {
	Run() error
}

type deviceID uint64

// Reset clears the device state.
func (d *Device) Reset() error {
	return nil
}

func (d Device) String() string { return d.ID }

// NewDevice allocates a Device.
func NewDevice(id string) *Device {
	return &Device{ID: id}
}

func internalHelper() {}
'''


def _by_qname(source: str):
    from wright_index.parsers import get_parser
    data = source.encode()
    tree = get_parser("go").parse(data)
    return {s.qualified_name: s for s in extract(data, tree)}


def test_finds_all_declarations():
    syms = _by_qname(SAMPLE)
    assert set(syms) == {
        "Device", "Runner", "deviceID",
        "Device.Reset", "Device.String", "NewDevice", "internalHelper",
    }


def test_type_classification():
    syms = _by_qname(SAMPLE)
    assert syms["Device"].kind == "struct"
    assert syms["Runner"].kind == "interface"
    assert syms["deviceID"].kind == "type"             # named basic type


def test_methods_qualified_by_receiver():
    syms = _by_qname(SAMPLE)
    reset = syms["Device.Reset"]
    assert reset.kind == "method"
    assert reset.parent == "Device"                    # pointer receiver stripped
    assert syms["Device.String"].parent == "Device"    # value receiver too


def test_doc_comments_multiline_and_marker_stripped():
    syms = _by_qname(SAMPLE)
    # two adjacent // lines merge into one doc; "//" markers removed
    assert syms["Device"].docstring == (
        "Device represents one accelerator card.\nIt is safe for concurrent use.")
    assert syms["NewDevice"].docstring == "NewDevice allocates a Device."
    assert syms["deviceID"].docstring is None          # no comment above


def test_exported_is_gos_actual_capitalization_rule():
    syms = _by_qname(SAMPLE)
    assert syms["Device"].is_exported
    assert syms["NewDevice"].is_exported
    assert not syms["deviceID"].is_exported
    assert not syms["internalHelper"].is_exported


def test_signatures():
    syms = _by_qname(SAMPLE)
    assert syms["Device"].signature == "type Device struct"
    assert syms["Runner"].signature == "type Runner interface"
    assert syms["Device.Reset"].signature == "func (d *Device) Reset() error"
    assert syms["deviceID"].signature == "type deviceID uint64"
