#!/usr/bin/env python
"""Regenerate fitparse/profile.py from Garmin's official Python FIT SDK.

generate_profile.py reads Profile.xlsx from the FIT SDK zip, but that zip is behind a licence
click-through on developer.garmin.com. Garmin publishes the same profile, as a Python dict, in
its open FIT Python SDK (github.com/garmin/fit-python-sdk, under the same FIT Protocol License).
This script converts that dict into generate_profile.py's structures and writes profile.py with
generate_profile.py's own renderer, so the output has the same shape as before.

    curl -L -o sdk.tar.gz https://github.com/garmin/fit-python-sdk/archive/refs/tags/<version>.tar.gz
    tar xzf sdk.tar.gz
    python scripts/generate_profile_from_sdk.py fit-python-sdk-<version>/garmin_fit_sdk/profile.py fitparse/profile.py

What the dict does not carry, and how it is filled in:
  * a base type per type: taken from the fields that use the type, else from the profile being
    replaced, else the smallest unsigned type that holds its values;
  * per-component accumulation: a component accumulates when its target field does (this is
    what Garmin's own decoder does);
  * message groups (only section headers in the output): taken from the profile being replaced;
  * comments: the dict has none.
"""
import importlib.util
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import generate_profile as gp  # noqa: E402  (its renderer and data structures)


def load_sdk_profile(path):
    spec = importlib.util.spec_from_file_location('garmin_fit_sdk_profile', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.Profile


def old_profile_info(path):
    """Base types and message groups from the profile.py being replaced, if there is one."""
    base_types, groups = {}, {}
    if not path or not os.path.exists(path):
        return base_types, groups
    text = open(path, encoding='utf-8').read()
    for name, base in re.findall(r"    '(\w+)': FieldType\([^\n]*\n\s+name='\w+',\n\s+base_type=BASE_TYPES\[0x\w+\],  # (\w+)", text):
        base_types[name] = base
    group = None
    for line in text[text.index('MESSAGE_TYPES = {'):].splitlines():
        m = re.match(r'    # \*+ (.+?) \*+$', line)
        if m:
            group = m.group(1)
            continue
        m = re.match(r'    (\d+): MessageType\(', line)
        if m and group:
            groups[int(m.group(1))] = group
    return base_types, groups


def _first(value):
    if isinstance(value, list):
        return value[0] if value else None
    return value


def _units(value):
    value = _first(value)
    return gp.fix_units(value) if value else None


def _per_component(values, n):
    """One value per component. The spreadsheet lets one value stand for all of them, which the
    SDK dict keeps as [value, '', '', ...] (hr.event_timestamp_12 units: ['s', '', ...])."""
    values = values if isinstance(values, list) else [values]
    if len(values) == 1 or (values and all(v in ('', None) for v in values[1:])):
        return [values[0]] * n
    return values


def _components(sdk_field, fields_by_num):
    out, bit_offset = [], 0
    n = len(sdk_field['components'])
    units = _per_component(sdk_field['units'], n)
    for i, target_num in enumerate(sdk_field['components']):
        target = fields_by_num[target_num]
        bits = sdk_field['bits'][i]
        out.append(gp.ComponentFieldInfo(
            name=target['name'], num=target_num, scale=gp.fix_scale(sdk_field['scale'][i]),
            offset=sdk_field['offset'][i] or None, units=_units(units[i]),
            bits=bits, bit_offset=bit_offset, accumulate=bool(target['is_accumulated'])))
        bit_offset += bits
    return out


def _scale_offset_units(sdk_field, components):
    # As in the spreadsheet: a field with one component keeps its own scale (altitude is scale 5,
    # offset 500, and also feeds enhanced_altitude); with several, the components carry them.
    if len(components) > 1:
        return None, None, None
    return gp.fix_scale(_first(sdk_field['scale'])), (_first(sdk_field['offset']) or None), _units(sdk_field['units'])


def convert(profile, old_base_types, old_groups):
    sdk_types = profile['types']

    # Base type of each type: from a field declared with it, else from the profile being replaced,
    # else from a field it is only a subfield of. A subfield's parent can be wider than the type
    # (event.data is uint32; its timer_trigger subfield is an enum), so that comes last.
    direct, via_subfield = {}, {}
    for mesg in profile['messages'].values():
        for f in mesg['fields'].values():
            direct.setdefault(f['type'], f['base_type'])
            for sub in f['sub_fields']:
                via_subfield.setdefault(sub['type'], f['base_type'])

    def base_type(name, values):
        if direct.get(name) in gp.BASE_TYPES:
            return direct[name]
        if name in old_base_types:
            return old_base_types[name]
        if via_subfield.get(name) in gp.BASE_TYPES:
            return via_subfield[name]
        top = max(values, default=0)
        return 'enum' if top < 0xFF else 'uint16' if top < 0xFFFF else 'uint32'

    type_list = gp.TypeList([])
    for name, values in sdk_types.items():
        info = gp.TypeInfo(name=name, base_type=base_type(name, values), values=[], comment=None)
        for value, value_name in values.items():
            if f'{name}:{value_name}' not in gp.IGNORE_TYPE_VALUES:
                info.values.append(gp.TypeValueInfo(name=value_name, value=int(value), comment=None))
        type_list.types.append(info)
    if not type_list.get('bool', raise_exception=False):
        type_list.types.append(gp.TypeInfo('bool', 'enum', [], None))

    message_list = gp.MessageList([])
    for mesg in profile['messages'].values():
        num = int(mesg['num'])
        fields_by_num = {int(k): v for k, v in mesg['fields'].items()}
        message = gp.MessageInfo(name=mesg['name'], num=num, group_name=old_groups.get(num, 'Other Messages'),
                                 fields=[], comment=None)
        for field_num, f in sorted(fields_by_num.items()):
            components = _components(f, fields_by_num)
            scale, offset, units = _scale_offset_units(f, components)
            field = gp.FieldInfo(name=f['name'], type=f['type'], num=field_num, scale=scale, offset=offset,
                                 units=units, components=components, subfields=[], comment=None)
            for sub in f['sub_fields']:
                refs = [gp.ReferenceFieldInfo(name=r['name'], value=r['value_name'], num=int(r['num']),
                                              raw_value=int(r['raw_value'])) for r in sub['map']]
                # As generate_profile.py: alert_type references are left out.
                if any(r.name == 'alert_type' for r in refs):
                    continue
                sub_components = _components(sub, fields_by_num)
                s_scale, s_offset, s_units = _scale_offset_units(sub, sub_components)
                field.subfields.append(gp.SubFieldInfo(
                    name=sub['name'], num=field_num, type=sub['type'], scale=s_scale, offset=s_offset,
                    units=s_units, ref_fields=refs, components=sub_components, comment=None))
            if field_num == gp.FIELD_NUM_TIMESTAMP and (field.components or field.subfields):
                raise AssertionError(f"{mesg['name']}.timestamp has components or subfields")
            message.fields.append(field)
        message_list.messages.append(message)
    return type_list, message_list


def main(sdk_profile_path, output_py_path=None):
    profile = load_sdk_profile(sdk_profile_path)
    version = '{major}.{minor}'.format(**profile['version'])
    old_base_types, old_groups = old_profile_info(output_py_path)
    if output_py_path and os.path.exists(output_py_path):
        if not open(output_py_path, encoding='utf-8').read().strip().startswith(gp.PROFILE_HEADER_FIRST_PART):
            print("Python file doesn't begin with appropriate header. Exiting.")
            sys.exit(1)
    type_list, message_list = convert(profile, old_base_types, old_groups)
    gp.write_profile(type_list, message_list, version, output_py_path)


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print(f'Usage: {os.path.basename(__file__)} <garmin_fit_sdk/profile.py> [fitparse/profile.py]')
        sys.exit(0)
    main(sys.argv[1], sys.argv[2] if len(sys.argv) >= 3 else None)
