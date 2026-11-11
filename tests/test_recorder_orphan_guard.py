"""Exercise orphan safety, cached bindings and strict source replacement."""
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from sqlalchemy import BigInteger, Column, ForeignKey, create_engine, delete, lambda_stmt, select
from sqlalchemy.dialects import mysql
from sqlalchemy.orm import DeclarativeBase

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('recorder_orphan_guard', ROOT / 'helm/recorder-orphan-guard/patch.py')
GUARD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(GUARD)
# These independent fixture functions match the upstream helpers. Only the
# fixture hash is substituted in unit tests; production accepts the full file hash.
SOURCE = '''def delete_event_data_rows(data_ids):
    return lambda_stmt(
        lambda: (
            delete(EventData)
            .where(EventData.data_id.in_(data_ids))
            .execution_options(synchronize_session=False)
        )
    )

def delete_states_attributes_rows(attributes_ids):
    return lambda_stmt(
        lambda: (
            delete(StateAttributes)
            .where(StateAttributes.attributes_id.in_(attributes_ids))
            .execution_options(synchronize_session=False)
        )
    )
'''


class Base(DeclarativeBase):
    pass


class StateAttributes(Base):
    __tablename__ = 'state_attributes'
    attributes_id = Column(BigInteger, primary_key=True)


class States(Base):
    __tablename__ = 'states'
    state_id = Column(BigInteger, primary_key=True)
    attributes_id = Column(BigInteger, ForeignKey('state_attributes.attributes_id'))


class EventData(Base):
    __tablename__ = 'event_data'
    data_id = Column(BigInteger, primary_key=True)


class Events(Base):
    __tablename__ = 'events'
    event_id = Column(BigInteger, primary_key=True)
    data_id = Column(BigInteger, ForeignKey('event_data.data_id'))


class RecorderOrphanGuardTests(unittest.TestCase):
    def setUp(self):
        self.pin = patch.object(GUARD, 'SOURCE_SHA256', GUARD.digest(SOURCE))
        self.pin.start()
        self.addCleanup(self.pin.stop)
        self.source, status = GUARD.patched_source(SOURCE)
        self.assertEqual(status, 'patched')
        self.scope = dict(lambda_stmt=lambda_stmt, delete=delete, select=select,
                          StateAttributes=StateAttributes, States=States, EventData=EventData, Events=Events)
        exec(self.source, self.scope)

    def test_orphan_only_deletion_and_distinct_cached_candidates(self):
        engine = create_engine('sqlite://')
        self.addCleanup(engine.dispose)
        Base.metadata.create_all(engine)
        with engine.begin() as c:
            c.exec_driver_sql('PRAGMA foreign_keys=ON')
            for parent, child, parent_key, child_key, helper in [
                (StateAttributes, States, 'attributes_id', 'state_id', 'delete_states_attributes_rows'),
                (EventData, Events, 'data_id', 'event_id', 'delete_event_data_rows'),
            ]:
                c.execute(parent.__table__.insert(), [{parent_key: n} for n in range(1, 6)])
                c.execute(child.__table__.insert(), [{child_key: 11, parent_key: 1}])
                for ids, expected in [({1, 2}, 1), ({3, 4}, 2), ({1, 5}, 1), ({1}, 0)]:
                    self.assertEqual(c.execute(self.scope[helper](ids)).rowcount, expected)
                self.assertEqual(c.execute(select(getattr(parent, parent_key))).scalars().all(), [1])
                self.assertEqual(c.execute(select(getattr(child, parent_key))).scalars().all(), [1])

    def test_mysql_correlated_guard_and_bound_parameter_shapes(self):
        for helper, parent, child, key in [
            ('delete_states_attributes_rows', 'state_attributes', 'states', 'attributes_id'),
            ('delete_event_data_rows', 'event_data', 'events', 'data_id'),
        ]:
            for size in (1, 999, 4000):
                ids = set(range(1, size + 1))
                compiled = self.scope[helper](ids).compile(dialect=mysql.dialect(), compile_kwargs={'render_postcompile': True})
                sql = str(compiled)
                self.assertIn('NOT (EXISTS (SELECT', sql)
                self.assertIn(f'FROM {child}', sql)
                self.assertIn(f'{child}.{key} = {parent}.{key}', sql)
                self.assertNotIn(f'FROM {child}, {parent}', sql)
                self.assertEqual(set(compiled.params.values()), ids)

    def test_unknown_and_partially_modified_source_is_untouched(self):
        for value in (SOURCE + '\n', self.source + '# changed\n', SOURCE.replace('delete(EventData)', 'delete(Events)')):
            self.assertEqual(GUARD.patched_source(value), (value, 'unsupported'))

    def test_idempotent_atomic_replacement_and_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'queries.py'
            target.write_text(SOURCE)
            target.chmod(0o640)
            self.assertEqual(GUARD.patch_file(target), 'patched')
            self.assertEqual(target.read_text(), self.source)
            self.assertEqual(GUARD.patch_file(target), 'already-patched')
            self.assertEqual(target.stat().st_mode & 0o777, 0o640)
            link = Path(directory) / 'linked.py'
            link.symlink_to(target)
            self.assertEqual(GUARD.patch_file(link), 'unsupported')

    def test_primary_and_recovery_mounts_are_present(self):
        primary = (ROOT / 'helm/recorder-orphan-guard/values.yaml').read_text()
        recovery = (ROOT / 'helm/native-recovery/templates/core.yaml').read_text()
        installer = (ROOT / 'scripts/restore-native-backup.py').read_text()
        self.assertIn('/etc/cont-init.d/53-recorder-orphan.py', primary)
        self.assertIn('/components/recorder/queries.py, subPath: recorder-queries.py', recovery)
        self.assertIn('ha-recorder-orphan-guard', installer)


if __name__ == '__main__':
    unittest.main()
