import datetime
import math
import struct


class RecordBase(object):
    # namedtuple-like base class. Subclasses should must __slots__
    __slots__ = ()

    def __init__(self, *args, **kwargs):
        for slot_name, value in zip(self.__slots__, args):
            setattr(self, slot_name, value)
        for slot_name, value in kwargs.iteritems():
            setattr(self, slot_name, value)


class MessageHeader(RecordBase):
    __slots__ = ('is_definition', 'local_mesg_type', 'time_offset')


class DefinitionMessage(RecordBase):
    __slots__ = ('header', 'endian', 'mesg_type', 'mesg_num', 'field_defs')
    type = 'definition'

    @property
    def name(self):
        return self.mesg_type.name if self.mesg_type else None


class FieldDefinition(RecordBase):
    __slots__ = ('field', 'def_num', 'base_type', 'size')

    @property
    def name(self):
        return self.field.name if self.field else None


class DataMessage(RecordBase):
    __slots__ = ('header', 'def_mesg', 'fields')
    type = 'data'

    def get(self, field_name, as_dict=True):
        for field_data in self.fields:
            if field_data.is_named(field_name):
                return field_data.as_dict() if as_dict else field_data

    def get_value(self, field_name):
        field_data = self.get(field_name, as_dict=False)
        if field_data:
            return field_data.value

    def get_values(self):
        return dict((f.name if f.name else f.def_num, f.value) for f in self.fields)

    @property
    def name(self):
        return self.def_mesg.name

    @property
    def mesg_num(self):
        return self.def_mesg.mesg_num

    def as_dict(self):
        return {
            'name': self.name,
            'fields': [f.as_dict() for f in self.fields],
        }

    def __str__(self):
        return '%s (#%d)' % (self.name if self.name else 'unknown', self.mesg_num)


class FieldData(RecordBase):
    __slots__ = ('field_def', 'field', 'parent_field', 'value', 'raw_value')

    @property
    def name(self):
        return self.field.name if self.field else None

    # TODO: Some notion of flags

    def is_named(self, name):
        if self.field:
            if name in (self.field.name, self.field.def_num):
                return True
        if self.parent_field:
            if name in (self.parent_field.name, self.parent_field.def_num):
                return True
        if self.field_def:
            if name == self.field_def.def_num:
                return True
        return False

    @property
    def def_num(self):
        # Prefer to return the def_num on the field
        # since field_def may be None if this field is dynamic
        return self.field.def_num if self.field else self.field_def.def_num

    @property
    def base_type(self):
        # Try field_def's base type, if it doesn't exist, this is a
        # dynamically added field, so field doesn't be None
        return self.field_def.base_type if self.field_def else self.field.base_type

    @property
    def is_base_type(self):
        return self.field.is_base_type if self.field else True

    @property
    def type(self):
        return self.field.type if self.field else self.base_type

    @property
    def field_type(self):
        return self.field.field_type if self.field else 'field'

    @property
    def units(self):  # TODO: have this be set here, so we can make custom units for converters
        return self.field.units if self.field else None

    def as_dict(self):
        return {
            'name': self.name, 'def_num': self.def_num, 'base_type': self.base_type.name,
            'type': self.type.name, 'units': self.units, 'value': self.value,
            'raw_value': self.raw_value,
        }

    def __iter__(self):
        for field in sorted(
            self.fields, key=lambda f: (int(f.name is None), f.name, f.def_num),
        ):
            if not field.components:
                yield field

    def __str__(self):
        return '%s: %s%s' % (
            self.name if self.name else 'unknown-%d' % self.def_num,
            self.value, ' [%s]' % self.units if self.units else '',
        )


class BaseType(RecordBase):
    __slots__ = ('name', 'num', 'fmt', 'parse')
    values = None  # In case we're treated as a FieldType

    @property
    def size(self):
        return struct.calcsize(self.fmt)


# Bare minimum profile.py to make generate_profile.py work

class FieldType(RecordBase):
    __slots__ = ('name', 'base_type', 'values')


class MessageType(RecordBase):
    __slots__ = ('name', 'mesg_num', 'fields')


class FieldBase(RecordBase):
    __slots__ = ()

    @property
    def base_type(self):
        return self.type if self.is_base_type else self.type.base_type

    @property
    def is_base_type(self):
        return isinstance(self.type, BaseType)

    def render(self, raw_value):
        if self.type.values and (raw_value in self.type.values):
            return self.type.values[raw_value]

        # Apply custom rendering function
        # TODO: This is the wrong place for this, do this in FitFile
        # by making a FitFileUnitConverter standard class that is subclassable
        # Then, have FieldData have a custom units field
        render_type = getattr(self, "render_%s" % self.type.name, None)
        if render_type:
            raw_value = render_type(raw_value)

        return raw_value

    # Custom Field Rendering Functions

    def render_bool(self, raw_value):
        return bool(raw_value)

    # TODO: maybe don't convert units for now

    def render_date_time(self, raw_value):
        if raw_value is not None and raw_value >= 0x10000000:
            raw_value = datetime.datetime.utcfromtimestamp(631065600 + raw_value)
        return raw_value
    render_date_time.units = None  # TODO: propagate this
    # Idea: have namedtuple have a field base_units, and have FieldData.units
    # be a property to actually figure out what they should be

    def render_local_date_time(self, raw_value):
        if raw_value is not None:
            raw_value = datetime.datetime.fromtimestamp(631065600 + raw_value)
        return raw_value


class SubField(FieldBase):
    __slots__ = ('name', 'def_num', 'type', 'scale', 'offset', 'units', 'ref_fields')
    field_type = 'subfield'


class ReferenceField(RecordBase):
    __slots__ = ('name', 'def_num', 'value', 'raw_value')


class ComponentField(RecordBase):
    __slots__ = ('name', 'def_num', 'scale', 'offset', 'units', 'accumulate', 'bits', 'bit_offset')
    field_type = 'component'

    def render(self, raw_value):
        if raw_value is None:
            return None

        # If it's a tuple, then it's a byte array and unpack it as such
        # (only type that uses this is compressed speed/distance)
        if isinstance(raw_value, tuple):
            unpacked_num = 0
            # Unpack byte array as little endian
            for value in reversed(raw_value):
                unpacked_num = (unpacked_num << 8) + value
            # Shift according to bit offset, mask according to bits
            raw_value = (unpacked_num >> self.bit_offset) & ((1 << self.bits) - 1)

        return raw_value


class Field(FieldBase):
    __slots__ = ('name', 'type', 'def_num', 'scale', 'offset', 'units', 'components', 'subfields')
    field_type = 'field'


# The default base type
BASE_TYPE_BYTE = BaseType(name='byte', num=0x0D, fmt='B', parse=lambda x: None if all(b == 0xFF for b in x) else x)

BASE_TYPES = {
    0x00: BaseType(name='enum', num=0x00, fmt='B', parse=lambda x: None if x == 0xFF else x),
    0x01: BaseType(name='sint8', num=0x01, fmt='b', parse=lambda x: None if x == 0x7F else x),
    0x02: BaseType(name='uint8', num=0x02, fmt='B', parse=lambda x: None if x == 0xFF else x),
    0x83: BaseType(name='sint16', num=0x83, fmt='h', parse=lambda x: None if x == 0x7FFF else x),
    0x84: BaseType(name='uint16', num=0x84, fmt='H', parse=lambda x: None if x == 0xFFFF else x),
    0x85: BaseType(name='sint32', num=0x85, fmt='i', parse=lambda x: None if x == 0x7FFFFFFF else x),
    0x86: BaseType(name='uint32', num=0x86, fmt='I', parse=lambda x: None if x == 0xFFFFFFFF else x),
    0x07: BaseType(name='string', num=0x07, fmt='s', parse=lambda x: x.split('\x00')[0] or None),
    0x88: BaseType(name='float32', num=0x88, fmt='f', parse=lambda x: None if math.isnan(x) else x),
    0x89: BaseType(name='float64', num=0x89, fmt='d', parse=lambda x: None if math.isnan(x) else x),
    0x0A: BaseType(name='uint8z', num=0x0A, fmt='B', parse=lambda x: None if x == 0x0 else x),
    0x8B: BaseType(name='uint16z', num=0x8B, fmt='H', parse=lambda x: None if x == 0x0 else x),
    0x8C: BaseType(name='uint32z', num=0x8C, fmt='I', parse=lambda x: None if x == 0x0 else x),
    0x0D: BASE_TYPE_BYTE,
}