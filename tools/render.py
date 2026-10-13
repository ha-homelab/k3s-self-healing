#!/usr/bin/env python3
"""Render a selected component with a validated local installation profile."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import yaml

from site_profile import load_profile, materialize, recovery_objects

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile', type=Path, required=True)
    parser.add_argument('component', choices=['helm', 'companions', 'recovery', 'updater-settings',
                                            'esphome', 'ring-mqtt', 'music-assistant'])
    args = parser.parse_args()
    profile = load_profile(args.profile)
    if args.component == 'updater-settings':
        settings = json.loads((ROOT / 'core_updates/settings.example.json').read_text())
        print(json.dumps(materialize(settings, profile), indent=2))
    elif args.component == 'helm':
        print(yaml.safe_dump(materialize(yaml.safe_load((ROOT / 'helm/values-pajikos.yaml').read_text()), profile), sort_keys=False))
    elif args.component in {'esphome', 'ring-mqtt', 'music-assistant'}:
        names = {'esphome': '040-esphome.yaml', 'ring-mqtt': '045-ring-mqtt.yaml',
                 'music-assistant': '050-music-assistant.yaml'}
        objects = list(yaml.safe_load_all((ROOT / 'companions' / names[args.component]).read_text()))
        sys.stdout.write(yaml.safe_dump_all(materialize(objects, profile), sort_keys=False))
    else:
        path = 'disaster_recovery' if args.component == 'recovery' else 'companions'
        objects = list(yaml.safe_load_all(subprocess.check_output(['kubectl', 'kustomize', str(ROOT / path)], text=True)))
        if args.component == 'recovery':
            objects = recovery_objects(objects, profile)
        else:
            objects = materialize(objects, profile)
        sys.stdout.write(yaml.safe_dump_all(objects, sort_keys=False))


if __name__ == '__main__':
    main()
