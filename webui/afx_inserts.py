"""Load and remove effects in a device's insert racks (Zen Go, Discrete 4).

Driven by the profile's ``afx_inserts`` block. Every change reads the rack
fresh, rewrites the whole rack the way the vendor panel does, and reads it
back. A mismatch stops further writes for this server session.
"""
from concurrent.futures import Future, TimeoutError
import time

from antelope import protocol

STRIP_CATEGORY = 0x19
REMAINING_CATEGORY = 0x15
LINK_CATEGORY, LINK_INDEX = 0x0b, 4


class InsertRack:
    def __init__(self, device):
        self.device = device
        self.failure = None

    def _spec(self):
        if protocol.afx_insert_writes(self.device.profile) is None:
            return None
        return self.device.profile['afx_inserts']

    def _strip(self, spec, channel):
        index = spec.get('record_index_for_channel', {}).get(str(channel))
        if type(channel) is not int or index is None:
            raise ValueError('That channel has no insert rack')
        if index not in spec['writes']['strips']:
            raise ValueError('Editing is not verified for that rack yet')
        return int(index)

    def state(self):
        spec = self._spec()
        if spec is None:
            return {'available': False}
        device, writes = self.device, spec['writes']
        labels = spec.get('type_labels', {})
        with device._lock:
            online = bool(device.snapshot.get('online'))
            racks = {}
            for channel in spec['mixer_channels']:
                index = spec['record_index_for_channel'][str(channel)]
                rows = device.structured.get((STRIP_CATEGORY, index))
                racks[str(channel)] = None if rows is None else [
                    {'type': row['type'], 'instance': row['inst'],
                     'label': labels.get(str(row['type']), {}).get('label', f"type {row['type']}")}
                    for row in rows if row['type']]
        return {
            'available': True, 'online': online,
            'writes_enabled': self.failure is None, 'failure': self.failure,
            'max_slots': writes['max_slots'],
            'editable_channels': [channel for channel in spec['mixer_channels']
                                  if spec['record_index_for_channel'][str(channel)] in writes['strips']],
            'racks': racks,
            'mapped_types': sorted(int(kind) for kind, effect in
                                   ((spec.get('parameters') or {}).get('effects') or {}).items()
                                   if (spec.get('parameters') or {}).get('enabled')
                                   and effect.get('editable') is not False),
            'effects': [{'type': int(kind), 'label': labels.get(kind, {}).get('label', f'type {kind}'),
                         'group': labels.get(kind, {}).get('group', '')}
                        for kind in writes['effects']],
        }

    def _query(self, transport, category, index):
        profile = self.device.profile
        request = protocol.build_readback_query(profile, category, index)
        response = transport.query(request, lambda data: protocol.is_readback_response(
            profile, data, category, index), timeout=1.5)
        if response is None:
            raise RuntimeError('The device did not answer; nothing was changed')
        return protocol.readback_body(profile, response)

    def _read_chain(self, transport, strip):
        device = self.device
        records = protocol.parse_afx_strip_order(
            device.profile, self._query(transport, STRIP_CATEGORY, strip), STRIP_CATEGORY, strip)
        slots = [(row['type'], row['inst']) for row in records]
        loaded = [slot for slot in slots if slot[0]]
        if slots[:len(loaded)] != loaded:
            raise RuntimeError('The rack has a gap between effects; edit it in the vendor panel')
        with device._lock:
            device.structured[(STRIP_CATEGORY, strip)] = records
            device.rb_ver += 1
        return loaded

    def _run(self, work):
        device = self.device
        with device._lock:
            if not device.snapshot.get('online') or device._transport is None:
                raise RuntimeError('The device is offline')
        future = Future()

        def queued(transport):
            if not future.set_running_or_notify_cancel():
                return
            try:
                future.set_result(work(transport))
            except Exception as error:
                future.set_exception(error)
                if isinstance(error, OSError):
                    raise
        device.submit(queued)
        try:
            return future.result(timeout=12)
        except TimeoutError:
            future.cancel()
            raise RuntimeError('The rack change timed out; check the rack before retrying') from None

    # ---- effect parameters -------------------------------------------
    def _read_reply(self, transport, type_id):
        profile = self.device.profile
        response = transport.query(
            protocol.build_afx_state_query(profile, type_id),
            lambda data: protocol.is_afx_state_response(profile, data, type_id), timeout=1.5)
        if response is None:
            raise RuntimeError('The device did not return the effect settings')
        body = protocol.readback_body(profile, response)
        self.last_reply = bytes(body)  # whole reply, shown by parameters() for mapping work
        return self.last_reply

    def _read_block(self, transport, type_id, instance, effect):
        body = self._read_reply(transport, type_id)
        # The device reports each instance as read_stride bytes; the writable
        # block sits read_offset bytes in (PowerGate: one leading on/off flag).
        length = int(effect['block_length'])
        start = instance * int(effect['read_stride']) + int(effect['read_offset'])
        block = bytes(body[start:start + length])
        if len(block) != length:
            raise RuntimeError('The effect settings came back too short')
        return block

    def _describe(self, effect, type_id, instance, block, spec):
        values = protocol.afx_block_get(effect, block)
        return {'ok': True, 'type': type_id, 'instance': instance,
                'label': spec.get('type_labels', {}).get(str(type_id), {}).get('label', f'type {type_id}'),
                'block': block.hex(' '),
                'fields': [{**{key: field[key] for key in ('key', 'label', 'min', 'max')},
                            'scale': field.get('scale', 1), 'unit': field.get('unit', ''),
                            # Knob step: integers move by one; a float field by 1/100 of its range.
                            'step': (field['max'] - field['min']) / 100 if field['type'] == 'f32' else 1,
                            'value': values[field['key']]} for field in effect['fields']]}

    def _slot(self, transport, spec, channel, slot):
        index = spec.get('record_index_for_channel', {}).get(str(channel))
        if type(channel) is not int or index is None:
            raise ValueError('That channel has no insert rack')
        chain = self._read_chain(transport, int(index))
        if type(slot) is not int or not 0 <= slot < len(chain):
            raise ValueError('There is no effect in that slot')
        type_id, instance = chain[slot]
        effect = protocol.afx_parameter_effect(self.device.profile, type_id)
        if effect is None:
            raise ValueError('The controls of this effect are not mapped yet')
        limit = spec['parameters'].get('max_instance')
        if limit is not None and instance > limit:
            raise ValueError('Only the first instance of an effect can be edited on this device so far')
        return type_id, instance, effect

    def parameters(self, channel, slot):
        spec = self._spec()
        if spec is None:
            raise ValueError('Insert editing is not available for this device')

        def work(transport):
            type_id, instance, effect = self._slot(transport, spec, channel, slot)
            if effect.get('editable') is False:
                # Read layout not confirmed: return the raw reply only, for mapping work.
                return {'ok': True, 'type': type_id, 'instance': instance, 'editable': False,
                        'label': spec.get('type_labels', {}).get(str(type_id), {}).get('label', f'type {type_id}'),
                        'fields': [], 'reply': self._read_reply(transport, type_id).hex(' ')}
            block = self._read_block(transport, type_id, instance, effect)
            return {**self._describe(effect, type_id, instance, block, spec),
                    'reply': self.last_reply.hex(' ')}
        return self._run(work)

    def set_parameter(self, channel, slot, key, value):
        spec = self._spec()
        if spec is None:
            raise ValueError('Insert editing is not available for this device')
        if self.failure:
            raise RuntimeError(self.failure)

        def work(transport):
            type_id, instance, effect = self._slot(transport, spec, channel, slot)
            if effect.get('editable') is False:
                raise ValueError('The settings of this effect cannot be read back yet, so they are not edited')
            before = self._read_block(transport, type_id, instance, effect)
            # A block whose mapped fields are out of range means the read layout is
            # wrong for this effect; writing it back would shift every setting.
            if any(not protocol.afx_field_in_range(field, value) for field, value in
                   zip(effect['fields'], protocol.afx_block_get(effect, before).values())):
                raise RuntimeError('The effect settings read back out of range; nothing was changed')
            after = protocol.afx_block_set(effect, before, key, value)
            if after != before:
                # One frame, or for a banded effect one frame per changed band.
                for frame in protocol.build_afx_parameter_writes(
                        self.device.profile, type_id, instance, after, before):
                    transport.write(frame)
                time.sleep(0.15)
                if self._read_block(transport, type_id, instance, effect) != after:
                    self.failure = ('An effect setting did not read back as written. Editing is off '
                                    'until the server restarts; check the effect in the vendor panel.')
                    raise RuntimeError(self.failure)
            return self._describe(effect, type_id, instance, after, spec)
        return self._run(work)

    def change(self, channel, operation, slot=None, type_id=None):
        spec = self._spec()
        if spec is None:
            raise ValueError('Insert editing is not available for this device')
        if self.failure:
            raise RuntimeError(self.failure)
        writes = spec['writes']
        strip = self._strip(spec, channel)
        if operation not in ('load', 'remove'):
            raise ValueError('Operation must be load or remove')
        effect = writes['effects'].get(str(type_id)) if operation == 'load' else None
        if operation == 'load' and effect is None:
            raise ValueError('That effect has no captured load on this device')

        def work(transport):
            if any(self._query(transport, LINK_CATEGORY, LINK_INDEX)):
                raise RuntimeError('A rack link is on; linked racks are not supported yet')
            before = self._read_chain(transport, strip)
            if operation == 'remove':
                if type(slot) is not int or not 0 <= slot < len(before):
                    raise ValueError('There is no effect in that slot')
                after = before[:slot] + before[slot + 1:]
            else:
                if len(before) >= writes['max_slots']:
                    raise RuntimeError(f"This rack already holds {writes['max_slots']} effects")
                used = {instance for kind, instance in before if kind == type_id}
                for other in spec['record_index_for_channel'].values():
                    if other != strip:
                        used.update(instance for kind, instance in
                                    self._read_chain(transport, other) if kind == type_id)
                with self.device._lock:
                    cached = dict(self.device.structured)
                for (category, index), rows in cached.items():
                    if category == STRIP_CATEGORY and index not in spec['record_index_for_channel'].values():
                        used.update(row['inst'] for row in rows if row['type'] == type_id)
                body = self._query(transport, REMAINING_CATEGORY, 0)
                remaining = dict(zip(body[0::2], body[1::2])).get(type_id, 0)
                if remaining < 1:
                    raise RuntimeError('The device reports no free instance of this effect')
                instance = next((value for value in effect['instances'] if value not in used), None)
                if instance is None:
                    raise RuntimeError('Every verified instance of this effect is already in use')
                after = before + [(type_id, instance)]
                # Built before anything is written: a bad profile entry must not leave a half load.
                frames, expected = protocol.afx_load_defaults(self.device.profile, type_id, instance)
            packet = protocol.build_afx_insert_chain(self.device.profile, strip, after)
            transport.write(packet)
            time.sleep(0.2)
            try:
                verified = self._read_chain(transport, strip) == after
            except RuntimeError:
                verified = False
            if not verified:
                self.failure = ('The rack did not read back as written. Insert editing is off '
                                'until the server restarts; check the rack in the vendor panel.')
                raise RuntimeError(self.failure)
            if operation == 'load':
                # The vendor panel follows some loads with the effect's default settings.
                for frame in frames:
                    transport.write(frame)
                    time.sleep(0.01)
                limit = (spec.get('parameters') or {}).get('max_instance')
                if frames and expected is not None and (limit is None or instance <= limit):
                    time.sleep(0.15)
                    params = protocol.afx_parameter_effect(self.device.profile, type_id)
                    if self._read_block(transport, type_id, instance, params) != expected:
                        self.failure = ('The default settings of the loaded effect did not read back as '
                                        'written. Insert editing is off until the server restarts; check '
                                        'the effect in the vendor panel.')
                        raise RuntimeError(self.failure)
            return self.state()
        return self._run(work)
