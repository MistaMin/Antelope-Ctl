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
            'effects': [{'type': int(kind), 'label': labels.get(kind, {}).get('label', f'type {kind}')}
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
            return self.state()
        return self._run(work)
