# +--------------------------------------------------------------------------+
# |  Licensed Materials - Property of IBM                                    |
# |                                                                          |
# | (C) Copyright IBM Corporation 2009-2018.                                 |
# +--------------------------------------------------------------------------+
# | This module complies with Django 1.0 and is                              |
# | Licensed under the Apache License, Version 2.0 (the "License");          |
# | you may not use this file except in compliance with the License.         |
# | You may obtain a copy of the License at                                  |
# | http://www.apache.org/licenses/LICENSE-2.0 Unless required by applicable |
# | law or agreed to in writing, software distributed under the License is   |
# | distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY |
# | KIND, either express or implied. See the License for the specific        |
# | language governing permissions and limitations under the License.        |
# +--------------------------------------------------------------------------+
# | Authors: Rahul Priyadarshi, Hemlata Bhatt, Vyshakh A                     |
# +--------------------------------------------------------------------------+

import copy
import datetime
import uuid

try:
    from django.db.backends.schema import BaseDatabaseSchemaEditor
except ImportError:
    from django.db.backends.base.schema import BaseDatabaseSchemaEditor

from django.db import models
from django.db.backends.utils import truncate_name
from django.db.models.fields.related import ManyToManyField
from django import VERSION as djangoVersion
from . import Database

Error = Database.Error


class DB2SchemaEditor(BaseDatabaseSchemaEditor):
    psudo_column_prefix = 'psudo_'
    sql_delete_table = "DROP TABLE %(table)s"
    sql_rename_table = "RENAME TABLE %(old_table)s TO %(new_table)s"
    sql_create_column = "ALTER TABLE %(table)s ADD COLUMN %(column)s %(definition)s"
    sql_alter_column_type = "ALTER COLUMN %(column)s SET DATA TYPE %(type)s"
    sql_alter_column_type_from_int_to_auto = "ALTER COLUMN %(column)s SET GENERATED BY DEFAULT AS IDENTITY( START WITH %(max)d, INCREMENT BY 1, CACHE 10 ORDER )"
    sql_create_fk = "ALTER TABLE %(table)s ADD CONSTRAINT %(name)s FOREIGN KEY (%(column)s) REFERENCES %(to_table)s (%(to_column)s)"
    sql_delete_pk = "ALTER TABLE %(table)s DROP CONSTRAINT %(name)s"
    sql_delete_unique = "ALTER TABLE %(table)s DROP CONSTRAINT %(name)s"
    sql_drop_pk = "ALTER TABLE %(table)s DROP PRIMARY KEY"
    sql_drop_default = "ALTER TABLE %(table)s ALTER COLUMN %(column)s DROP DEFAULT"

    @property
    def sql_create_pk(self):
        self._reorg_tables()
        return "ALTER TABLE %(table)s ADD CONSTRAINT %(name)s PRIMARY KEY (%(columns)s)"

    def prepare_default(self, value):
        return self.quote_value(value)

    def alter_field(self, model, old_field, new_field, strict=False):
        alter_field_data_type = False
        alter_field_nullable = False
        alter_field_default = False
        alter_field_primary_key = False
        alter_field_name = False
        alter_field_check_constraint = False
        alter_field_unique = False
        alter_field_index = False
        rebuild_incomming_fk = False
        alter_incomming_fk_data_type = False
        deferred_constraints = {
            'pk': {},
            'unique': {},
            'index': {},
            'check': {}
        }

        old_db_field = old_field.db_parameters(connection=self.connection)
        new_db_field = new_field.db_parameters(connection=self.connection)
        old_db_field_type = old_db_field['type']
        new_db_field_type = new_db_field['type']

        if old_field.remote_field is not None and hasattr(old_field.remote_field, 'through'):
            rel_condition = (
                        old_field.remote_field.through and new_field.remote_field.through and old_field.remote_field.through._meta.auto_created and new_field.remote_field.through._meta.auto_created)
        else:
            rel_condition = False

        if ((old_db_field_type, new_db_field_type) == (None, None)) and rel_condition:
            return self._alter_many_to_many(model, old_field, new_field, strict)
        elif old_db_field_type is None or new_db_field_type is None:
            raise ValueError("Cannot alter field %s into %s" % (
                old_db_field,
                new_db_field,
            ))

        # Need to change datatype which need remaking of field
        if (old_db_field_type != new_db_field_type) and (
                isinstance(old_field, (models.AutoField, models.TextField)) or isinstance(new_field, models.TextField)):
            if old_field.primary_key and new_field.primary_key:
                rebuild_incomming_fk = True
                if isinstance(old_field, models.AutoField) and isinstance(new_field, models.IntegerField):
                    pass
                else:
                    alter_incomming_fk_data_type = True
            old_field, new_field = self.alterFieldDataTypeByRemaking(model, old_field, new_field, strict)
            old_db_field = old_field.db_parameters(connection=self.connection)
            new_db_field = new_field.db_parameters(connection=self.connection)
            old_db_field_type = old_db_field['type']
            new_db_field_type = new_db_field['type']

        if old_db_field_type != new_db_field_type:
            alter_field_data_type = True

        if old_field.column != new_field.column:
            alter_field_name = True
        if old_field.db_index != new_field.db_index:
            alter_field_index = True
        if old_field.unique != new_field.unique:
            alter_field_unique = True
        if old_field.primary_key != new_field.primary_key:
            alter_field_primary_key = True
        if old_db_field['check'] != new_db_field['check']:
            alter_field_check_constraint = True
        if old_field.null != new_field.null:
            alter_field_nullable = True

        old_default = self.effective_default(old_field)
        new_default = self.effective_default(new_field)
        if (old_field.default is not None) and old_field.has_default():
            if old_default != new_default:
                alter_field_default = True

        # Need to remove Primary Key
        if alter_field_primary_key and old_field.primary_key:
            if strict:
                pk_names = self._constraint_names(model, [old_field.column], primary_key=True)
                if len(pk_names) == 0:
                    raise ValueError("Found no primary key in %s.%s " % (model._meta.db_table, old_field.column))
            self.execute(
                self.sql_drop_pk % {
                    'table': self.quote_name(model._meta.db_table)
                }
            )

        # Need to remove unique Key
        if alter_field_unique and old_field.unique or (
                old_field.unique and alter_field_primary_key and not old_field.primary_key):
            unique_key_names = self._constraint_names(model, [old_field.column], unique=True)
            if strict and len(unique_key_names) != 1:
                raise ValueError("Found wrong number of unique constraints for (table)s.(column)s" % {
                    'table': model._meta.db_table, 'column': old_field.column
                })

            for unique_key_name in unique_key_names:
                self.execute(
                    self.sql_delete_unique % {
                        'table': self.quote_name(model._meta.db_table),
                        'name': unique_key_name
                    }
                )

        # Need to remove Index
        if alter_field_index and old_field.db_index:
            index_names = self._constraint_names(model, [old_field.column], index=True)
            if strict and len(index_names) != 1:
                raise ValueError("Found wrong number of Indexes for (table)s.(column)s" % {
                    'table': model._meta.db_table, 'column': old_field.column
                })
            for index_name in index_names:
                self.execute(
                    self.sql_delete_index % {
                        'name': index_name
                    }
                )

        # Need to remove check constraint
        if alter_field_check_constraint and old_db_field['check']:
            check_constraint_names = self._constraint_names(model, [old_field.column], check=True)
            if strict and len(check_constraint_names) != 1:
                raise ValueError("Found wrong number of check constraints for (table)s.(column)s" % {
                    'table': model._meta.db_table, 'column': old_field.column
                })
            for check_constraint_name in check_constraint_names:
                self.execute(
                    self.sql_delete_check % {
                        'table': self.quote_name(model._meta.db_table),
                        'name': check_constraint_name
                    }
                )

        # Need to remove Nullability
        if alter_field_nullable and old_field.null:
            sql = self.sql_alter_column_not_null % {'column': self.quote_name(old_field.column)}
            self.execute(
                self.sql_alter_column % {
                    'table': self.quote_name(model._meta.db_table),
                    'changes': sql
                }
            )

        # Drop all FK constraints, if require we will make it again
        flag = old_field.remote_field
        if flag:
            fk_names = self._constraint_names(model, [old_field.column], foreign_key=True)
            for fk_name in fk_names:
                self.execute(
                    self.sql_delete_fk % {
                        'table': self.quote_name(model._meta.db_table),
                        'name': fk_name
                    }
                )

        if alter_field_name or alter_field_data_type:

            # Drop all incoming FK constraint, if require we will make it again
            if old_field.primary_key and new_field.primary_key:
                rebuild_incomming_fk = True
                for incoming_fks in old_field.model._meta.get_fields():
                    fk_names = self._constraint_names(incoming_fks.model, [incoming_fks.field.column], foreign_key=True)
                    for fk_name in fk_names:
                        self.execute(
                            self.sql_delete_fk % {
                                'table': self.quote_name(incoming_fks.model._meta.db_table),
                                'name': fk_name,
                            }
                        )

            # Defer constraint check
            with self.connection.cursor() as cur:
                constraints = self.connection.introspection.get_constraints(cur, model._meta.db_table)
            self._defer_constraints_check(constraints, deferred_constraints, old_field, new_field, model, defer_pk=True,
                                          defer_unique=True, defer_index=True, defer_check=True)

            # Need to change the field name
            if alter_field_name:
                self.execute(
                    self.sql_rename_column % {
                        'table': self.quote_name(model._meta.db_table),
                        'old_column': self.quote_name(old_field.column),
                        'new_column': self.quote_name(new_field.column),
                    }
                )

            # Need to change the field type
            if alter_field_data_type:
                if old_field.primary_key and new_field.primary_key:
                    if isinstance(new_field, models.AutoField) and isinstance(old_field, models.IntegerField):
                        pass
                    else:
                        alter_incomming_fk_data_type = True
                # Will make default later
                if (old_field.default is not None) and (old_field.has_default()) and (old_default is not None):
                    self.execute(self.sql_drop_default % {
                        'table': self.quote_name(model._meta.db_table),
                        'column': self.quote_name(new_field.column)
                    }
                                 )
                if isinstance(new_field, models.AutoField):
                    with self.connection.cursor() as cur:
                        cur.execute(
                            'SELECT MAX( %(column)s ) from %(table)s' % {
                                'column': self.quote_name(new_field.column),
                                'table': self.quote_name(model._meta.db_table),
                            }
                        )
                        max = cur.fetchone()
                        if max[0] is None:
                            max = 0
                        else:
                            max = max[0]
                    if not isinstance(old_field, models.IntegerField):
                        sql = self.sql_alter_column_type % {
                            'column': self.quote_name(new_field.column),
                            'type': 'Integer'
                        }
                        self.execute(
                            self.sql_alter_column % {
                                'table': self.quote_name(model._meta.db_table),
                                'changes': sql
                            }
                        )
                    sql = self.sql_alter_column_type_from_int_to_auto % {
                        'column': self.quote_name(new_field.column),
                        'max': max + 1
                    }
                    self.execute(
                        self.sql_alter_column % {
                            'table': self.quote_name(model._meta.db_table),
                            'changes': sql
                        }
                    )
                else:
                    sql = self.sql_alter_column_type % {
                        'column': self.quote_name(new_field.column),
                        'type': new_db_field_type
                    }
                    self.execute(
                        self.sql_alter_column % {
                            'table': self.quote_name(model._meta.db_table),
                            'changes': sql
                        }
                    )

            # restore constraint checks
            self._restore_constraints_check(deferred_constraints, old_field, new_field, model)

        # Need to change Default
        if alter_field_default:
            if new_default is None:
                if alter_field_data_type or alter_field_nullable:
                    pass
                else:
                    self.execute(self.sql_drop_default % {
                        'table': self.quote_name(model._meta.db_table),
                        'column': self.quote_name(new_field.column)
                    }
                                 )
            else:
                sql = self.sql_alter_column_default % {
                    'column': self.quote_name(new_field.column),
                    'default': self.prepare_default(new_default),
                }
                self.execute(
                    self.sql_alter_column % {
                        'table': self.quote_name(model._meta.db_table),
                        'changes': sql
                    }
                )

        # Need to change nullability
        if alter_field_nullable:
            sql = ""
            if new_field.null:
                sql = self.sql_alter_column_null % {
                    'column': self.quote_name(new_field.column)
                }
            else:
                sql = self.sql_alter_column_not_null % {
                    'column': self.quote_name(new_field.column)
                }
            self.execute(
                self.sql_alter_column % {
                    'table': self.quote_name(model._meta.db_table),
                    'changes': sql
                }
            )

        # Need to add check constraint
        if alter_field_check_constraint and new_db_field['check']:
            self.execute(
                self.sql_create_check % {
                    'table': self.quote_name(model._meta.db_table),
                    'name': self._create_index_name(model, [new_field.column], suffix="_check"),
                    'column': self.quote_name(new_field.column),
                    'check': new_db_field['check'],
                }
            )
        # Need to change incoming foreign key field type
        incoming_relations = []
        if alter_incomming_fk_data_type:
            incoming_relations.extend(new_field.model._meta.get_all_related_objects())

        # Need to add new PK
        if alter_field_primary_key and new_field.primary_key:
            # Drop old PK if available
            try:
                self.execute(
                    self.sql_drop_pk % {
                        'table': self.quote_name(model._meta.db_table)
                    }
                )
            except:
                pass
            self.__model = model
            self.execute(
                self.sql_create_pk % {
                    'table': self.quote_name(model._meta.db_table),
                    'name': self._create_index_name(model, [new_field.column], suffix="_pk"),
                    'columns': self.quote_name(new_field.column)
                }
            )
            # Need to update all incoming relations
            incoming_relations.extend(new_field.model._meta.get_all_related_objects())
        # Need to add a unique constraint
        elif alter_field_unique and new_field.unique:
            self.execute(
                self.sql_create_unique % {
                    'table': self.quote_name(model._meta.db_table),
                    'name': self._create_index_name(model._meta.db_table, [new_field.column], suffix="_uniq"),
                    'columns': self.quote_name(new_field.column),
                }
            )
            # Need to add a index
        elif alter_field_index and new_field.db_index:
            self.execute(
                self.sql_create_index % {
                    'table': self.quote_name(model._meta.db_table),
                    'name': self._create_index_name(model, [new_field.column], suffix="_index"),
                    'columns': self.quote_name(new_field.column),
                    'extra': "",
                }
            )
        # Update incoming FK field
        for inc_rel in incoming_relations:
            fk_db_field = inc_rel.field.db_parameters(connection=self.connection)
            fk_db_field_type = fk_db_field['type']
            sql = self.sql_alter_column_type % {
                'column': self.quote_name(inc_rel.field.column),
                'type': fk_db_field_type,
            }
            self.execute(
                self.sql_alter_column % {
                    'table': self.quote_name(inc_rel.model._meta.db_table),
                    'changes': sql
                }
            )

        # need to reorg table if we changed the field type of fk field
        if len(incoming_relations) > 0:
            self._reorg_tables()

        # Rebuild/make FK constraint, if it have any
        if (djangoVersion[0:2] < (1, 9)):
            if new_field.rel:
                self.execute(
                    self.sql_create_fk % {
                        'table': self.quote_name(model._meta.db_table),
                        'name': self._create_index_name(model, [new_field.column], suffix="_fk"),
                        'column': self.quote_name(new_field.column),
                        'to_table': self.quote_name(new_field.rel._meta.db_table),
                        'to_column': self.quote_name(new_field.rel.get_related_field().column),
                    }
                )
        else:
            if new_field.remote_field:
                self.execute(
                    self.sql_create_fk % {
                        'table': self.quote_name(model._meta.db_table),
                        'name': self._create_index_name(model, [new_field.column], suffix="_fk"),
                        'column': self.quote_name(new_field.column),
                        'to_table': self.quote_name(new_field.remote_field.model._meta.db_table),
                        'to_column': self.quote_name(new_field.remote_field.get_related_field().column),
                    }
                )
        # Rebuild incoming FK constraints
        if rebuild_incomming_fk:
            for inc_rel in new_field.model._meta.get_all_related_objects():
                self.execute(
                    self.sql_create_fk % {
                        'table': self.quote_name(inc_rel.model._meta.db_table),
                        'name': self._create_index_name(inc_rel.model, [inc_rel.field.column], suffix="_fk"),
                        'column': self.quote_name(inc_rel.field.column),
                        'to_table': self.quote_name(model._meta.db_table),
                        'to_column': self.quote_name(new_field.column),
                    }
                )

    def alterFieldDataTypeByRemaking(self, model, old_field, new_field, strict):
        tmp_new_field = copy.deepcopy(new_field)
        tmp_new_field.column = truncate_name("%s%s" % (self.psudo_column_prefix, tmp_new_field.column),
                                             self.connection.ops.max_name_length())
        self.add_field(model, tmp_new_field)

        # Transfer data from old field to new tmp field
        self.execute("UPDATE %s set %s=%s" % (
            self.quote_name(model._meta.db_table),
            self.quote_name(tmp_new_field.column),
            self.quote_name(old_field.column)
        )
                     )
        self.remove_field(model, old_field)
        return tmp_new_field, new_field

    def add_field(self, model, field):
        self.__model = model
        notnull = not field.null
        field.null = True
        p_key = field.primary_key
        field.primary_key = False
        unique = field.unique
        field._unique = False

        super(DB2SchemaEditor, self).add_field(model, field)
        if field.remote_field is not None and hasattr(field.remote_field, 'through'):
            rel_condition = field.remote_field.through._meta.auto_created
        else:
            rel_condition = False

        if isinstance(field, ManyToManyField) and rel_condition:
            return
        else:
            self._reorg_tables()
        sql = None
        if notnull or unique or p_key:
            del_column = self.sql_delete_column % {
                'table': self.quote_name(model._meta.db_table), 'column': self.quote_name(field.column)
            }
            if notnull:
                field.null = False
                sql = self.sql_alter_column_not_null % {'column': self.quote_name(field.column)}
                sql = self.sql_alter_column % {'table': self.quote_name(model._meta.db_table), 'changes': sql}
                try:
                    self.execute(sql)
                    self._reorg_tables()
                except Error as e:
                    self.execute(del_column)
                    raise e
            if p_key:
                field.primary_key = True
                cur = self.connection.cursor()
                # remove other pk if available
                for other_pk in cur.connection.primary_keys(True, cur.connection.get_current_schema(),
                                                            model._meta.db_table):
                    self.execute(
                        self.sql_delete_pk % {
                            'table': self.quote_name(model._meta.db_table),
                            'name': other_pk['PK_NAME']
                        }
                    )
                sql = self.sql_create_pk % {
                    'table': self.quote_name(model._meta.db_table),
                    'name': self._create_index_name(model, [field.column], suffix="_pk"),
                    'columns': self.quote_name(field.column)
                }
                try:
                    self.execute(sql)
                    self._reorg_tables()
                except Error as e:
                    self.execute(del_column)
                    raise e
            elif unique:
                field._unique = True
                constraint_name = self._create_index_name(model, [field.column], suffix="_uniq")
                sql = self.sql_create_unique % {
                    'table': self.quote_name(model._meta.db_table), 'name': constraint_name,
                    'columns': self.quote_name(field.column)
                }
                try:
                    self.execute(sql)
                    self._reorg_tables()
                except Error as e:
                    self.execute(del_column)
                    raise e

    def alter_db_table(self, model, old_db_table, new_db_table):
        super(DB2SchemaEditor, self).alter_db_table(model, old_db_table, new_db_table)

    def _alter_many_to_many(self, model, old_field, new_field, strict):
        deferred_constraints = {
            'pk': {},
            'unique': {},
            'index': {},
            'check': {}
        }

        if ((old_field.remote_field is not None and hasattr(old_field.remote_field, 'through')) and
                (new_field.remote_field is not None and hasattr(new_field.remote_field, 'through'))):
            old_field_rel_through = old_field.remote_field.through
            rel_old_field = old_field.remote_field.through._meta.get_field(old_field.m2m_reverse_field_name())
            rel_new_field = new_field.remote_field.through._meta.get_field(new_field.m2m_reverse_field_name())
        else:
            rel_old_field = None
            rel_new_field = None

        if ((rel_old_field is not None) and (rel_new_field is not None)):
            with self.connection.cursor() as cur:
                constraints = self.connection.introspection.get_constraints(cur, old_field_rel_through._meta.db_table)
            for constr_name, constr_dict in constraints.items():
                if constr_dict['foreign_key'] is not None:
                    self.execute(self.sql_delete_fk % {
                        "table": self.quote_name(old_field_rel_through._meta.db_table),
                        "name": constr_name,
                    })
            self._defer_constraints_check(constraints, deferred_constraints, rel_old_field, rel_new_field,
                                          old_field_rel_through, defer_pk=True, defer_unique=True, defer_index=True)

            self._reorg_tables()
            super(DB2SchemaEditor, self)._alter_many_to_many(model, old_field, new_field, strict)
            self._restore_constraints_check(deferred_constraints, rel_old_field, rel_new_field, new_field.rel.through)

    def _reorg_tables(self):
        checkReorgSQL = "select tabschema, tabname from sysibmadm.admintabinfo where reorg_pending = 'Y'"
        res = []
        reorgSQLs = []
        with self.connection.cursor() as cursor:
            cursor.execute(checkReorgSQL)
            res = cursor.fetchall()
        if res:
            for sName, tName in res:
                reorgSQL = '''CALL SYSPROC.ADMIN_CMD('REORG TABLE "%(sName)s"."%(tName)s"')''' % {
                    'sName': sName, 'tName': tName
                }
                reorgSQLs.append(reorgSQL)
        for sql in reorgSQLs:
            self.execute(sql)

    def _defer_constraints_check(self, constraints, deferred_constraints, old_field, new_field, model, defer_pk=False,
                                 defer_unique=False, defer_index=False, defer_check=False):
        for constr_name, constr_dict in constraints.items():
            if defer_pk and constr_dict['primary_key'] is True:
                if old_field.column in constr_dict['columns']:
                    self.execute(self.sql_delete_pk % {
                        'table': model._meta.db_table,
                        'name': constr_name
                    })
                    deferred_constraints['pk'][constr_name] = constr_dict['columns']
                    continue
            if defer_unique and constr_dict['unique'] is True:
                if old_field.column in constr_dict['columns']:
                    try:
                        self.execute(self.sql_delete_unique % {
                            'table': model._meta.db_table,
                            'name': constr_name
                        })
                        deferred_constraints['unique'][constr_name] = constr_dict['columns']
                        continue
                    except:
                        continue
            if defer_index and constr_dict['index'] is True:
                if old_field.column in constr_dict['columns']:
                    try:
                        self.execute(self.sql_delete_index % {
                            'table': model._meta.db_table,
                            'name': constr_name
                        })
                        deferred_constraints['index'][constr_name] = constr_dict['columns']
                    except:
                        pass
            if defer_check and constr_dict['check'] is True:
                if old_field.column in constr_dict['columns']:
                    self.execute(self.sql_delete_check % {
                        'table': model._meta.db_table,
                        'name': constr_name
                    })
                    deferred_constraints['check'][constr_name] = constr_dict['columns']

        return deferred_constraints

    def _restore_constraints_check(self, deferred_constraints, old_field, new_field, model):
        self.__model = model
        for pk_name, columns in deferred_constraints['pk'].items():
            self.execute(self.sql_create_pk % {
                'table': model._meta.db_table,
                'name': pk_name,
                'columns': ', '.join(column.replace(old_field.column, new_field.column) for column in columns)
            })
        for constr_name, columns in deferred_constraints['unique'].items():
            self.execute(self.sql_create_unique % {
                'table': model._meta.db_table,
                'name': constr_name,
                'columns': ', '.join(column.replace(old_field.column, new_field.column) for column in columns)
            })
        for index_name, columns in deferred_constraints['index'].items():
            self.execute(self.sql_create_index % {
                'table': model._meta.db_table,
                'name': index_name,
                'columns': ', '.join(column.replace(old_field.column, new_field.column) for column in columns),
                'extra': ""
            })

    def quote_value(self, value):
        if isinstance(value, (datetime.datetime, datetime.date, datetime.time, str)):
            escape_quotes = lambda s: f'{s}'.replace("'", "''")
            return f"'{escape_quotes(value)}'"
        elif isinstance(value, bool):
            return '1' if value else '0'
        elif isinstance(value, uuid.UUID):
            return f"'{value}'"
        elif isinstance(value, bytes):
            return f"BLOB(X'{value.hex()}')"
        elif isinstance(value, datetime.timedelta):
            # time intervals will be stored as double number of seconds
            return f"{value / datetime.timedelta(seconds=1)}"
        return str(value)
