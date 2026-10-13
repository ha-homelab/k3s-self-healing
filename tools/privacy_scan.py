#!/usr/bin/env python3
"""Check public source for identifiers, without echoing matched values."""
import argparse
import ipaddress
import json
from pathlib import Path
import re

PATTERNS = {
    'phone': re.compile(r'(?<!\w)\+\d[\d ()-]{8,}\d|\(\d{3}\)\s*\d{3}[- ]\d{4}'),
    'phone-field': re.compile(r'(?im)(?:phone_number|mobile_number|telephone)\s*[:=]\s*[\x22\x27]?\d{10,15}\b'),
    'hardware-address': re.compile(r'(?i)(?<![\w:])(?:[a-f0-9]{2}:){5}[a-f0-9]{2}(?![\w:])'),
    'workstation-path': re.compile(r'/(?:Users|home)/(?!(?:example-operator|runner|nonroot)(?:/|\b))[^/\s\x22\x27]+'),
    'cloud-resource-id': re.compile(r'ocid1\.[a-z0-9_.-]+'),
    'email': re.compile(r'[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}'),
    'ipv4': re.compile(r'(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])'),
}


def findings(name, text):
    result = []
    for rule, pattern in PATTERNS.items():
        for match in pattern.finditer(text):
            value = match.group()
            if rule == 'email' and (value == 'git@github.com' or value.endswith(
                    ('@example.com', '@example.invalid', '@users.noreply.github.com'))):
                continue
            if rule == 'ipv4':
                try:
                    address = ipaddress.IPv4Address(value)
                except ValueError:
                    continue
                # Documentation/benchmark fixtures, loopback, and generic k3s
                # defaults are not an operator's address inventory.
                if any(address in ipaddress.ip_network(network) for network in
                       ('192.0.2.0/24', '198.51.100.0/24', '203.0.113.0/24', '198.18.0.0/15', '127.0.0.0/8')):
                    continue
                if value in {'0.0.0.0', '10.0.0.0', '100.64.0.0', '169.254.0.0', '172.16.0.0',
                             '192.168.0.0', '224.0.0.0', '240.0.0.0', '10.43.0.0', '10.43.0.1'}:
                    continue
            result.append({'file': name, 'line': text.count('\n', 0, match.start()) + 1, 'rule': rule})
    return result


def scan(root, files):
    result = []
    for name in files:
        path = root / name
        if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root.resolve()):
            result.append({'file': name, 'rule': 'nonregular-source'})
            continue
        try:
            text = path.read_text()
        except UnicodeError:
            result.append({'file': name, 'rule': 'unreviewed-binary'})
            continue
        result.extend(findings(name, text))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('root', nargs='?', type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    files = json.loads((args.root / 'publication.json').read_text())['files']
    result = scan(args.root, files)
    if result:
        print(json.dumps(result, indent=2))
        raise SystemExit(1)
    print(f'Privacy patterns passed for {len(files)} approved files; manual contextual review is also required.')


if __name__ == '__main__':
    main()
