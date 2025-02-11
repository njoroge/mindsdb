import os
import shutil
import tempfile
import importlib
from pathlib import Path
from copy import deepcopy

from sqlalchemy import func

from mindsdb.interfaces.storage.db import session, Integration
from mindsdb.utilities.config import Config
from mindsdb.interfaces.storage.fs import FsStore
from mindsdb.utilities.fs import create_directory

from mindsdb.interfaces.file.file_controller import FileController
from mindsdb.interfaces.database.views import ViewController
from mindsdb.utilities.with_kwargs_wrapper import WithKWArgsWrapper


class IntegrationController:
    @staticmethod
    def _is_not_empty_str(s):
        return isinstance(s, str) and len(s) > 0

    def __init__(self):
        self._load_handler_modules()

    def add(self, name, data, company_id=None):
        if 'database_name' not in data:
            data['database_name'] = name
        if 'publish' not in data:
            data['publish'] = True

        bundle_path = data.get('secure_connect_bundle')
        if data.get('type') in ('cassandra', 'scylla') and self._is_not_empty_str(bundle_path):
            if os.path.isfile(bundle_path) is False:
                raise Exception(f'Can not get access to file: {bundle_path}')
            integrations_dir = Config()['paths']['integrations']

            p = Path(bundle_path)
            data['secure_connect_bundle'] = p.name

            integration_record = Integration(name=name, data=data, company_id=company_id)
            session.add(integration_record)
            session.commit()
            integration_id = integration_record.id

            folder_name = f'integration_files_{company_id}_{integration_id}'
            integration_dir = os.path.join(integrations_dir, folder_name)
            create_directory(integration_dir)
            shutil.copyfile(bundle_path, os.path.join(integration_dir, p.name))

            FsStore().put(
                folder_name,
                integration_dir,
                integrations_dir
            )
        elif data.get('type') in ('mysql', 'mariadb'):
            ssl = data.get('ssl')
            files = {}
            temp_dir = None
            if ssl is True:
                for key in ['ssl_ca', 'ssl_cert', 'ssl_key']:
                    if key not in data:
                        continue
                    if os.path.isfile(data[key]) is False:
                        if self._is_not_empty_str(data[key]) is False:
                            raise Exception("'ssl_ca', 'ssl_cert' and 'ssl_key' must be paths or inline certs")
                        if temp_dir is None:
                            temp_dir = tempfile.mkdtemp(prefix='integration_files_')
                        cert_file_name = data.get(f'{key}_name', f'{key}.pem')
                        cert_file_path = os.path.join(temp_dir, cert_file_name)
                        with open(cert_file_path, 'wt') as f:
                            f.write(data[key])
                        data[key] = cert_file_path
                    files[key] = data[key]
                    p = Path(data[key])
                    data[key] = p.name
            integration_record = Integration(name=name, data=data, company_id=company_id)
            session.add(integration_record)
            session.commit()
            integration_id = integration_record.id

            if len(files) > 0:
                integrations_dir = Config()['paths']['integrations']
                folder_name = f'integration_files_{company_id}_{integration_id}'
                integration_dir = os.path.join(integrations_dir, folder_name)
                create_directory(integration_dir)
                for file_path in files.values():
                    p = Path(file_path)
                    shutil.copyfile(file_path, os.path.join(integration_dir, p.name))
                FsStore().put(
                    folder_name,
                    integration_dir,
                    integrations_dir
                )
        else:
            integration_record = Integration(name=name, data=data, company_id=company_id)
            session.add(integration_record)
            session.commit()

    def modify(self, name, data, company_id):
        integration_record = session.query(Integration).filter_by(company_id=company_id, name=name).first()
        old_data = deepcopy(integration_record.data)
        for k in old_data:
            if k not in data:
                data[k] = old_data[k]

        integration_record.data = data
        session.commit()

    def delete(self, name, company_id=None):
        integration_record = session.query(Integration).filter_by(company_id=company_id, name=name).first()
        integrations_dir = Config()['paths']['integrations']
        folder_name = f'integration_files_{company_id}_{integration_record.id}'
        integration_dir = os.path.join(integrations_dir, folder_name)
        if os.path.isdir(integration_dir):
            shutil.rmtree(integration_dir)
        try:
            FsStore().delete(folder_name)
        except Exception:
            pass
        session.delete(integration_record)
        session.commit()

    def _get_integration_record_data(self, integration_record, sensitive_info=True):
        if integration_record is None or integration_record.data is None:
            return None
        data = deepcopy(integration_record.data)
        if data.get('password', None) is None:
            data['password'] = ''
        data['date_last_update'] = deepcopy(integration_record.updated_at)

        bundle_path = data.get('secure_connect_bundle')
        mysql_ssl_ca = data.get('ssl_ca')
        mysql_ssl_cert = data.get('ssl_cert')
        mysql_ssl_key = data.get('ssl_key')
        if (
            data.get('type') in ('mysql', 'mariadb')
            and (
                self._is_not_empty_str(mysql_ssl_ca)
                or self._is_not_empty_str(mysql_ssl_cert)
                or self._is_not_empty_str(mysql_ssl_key)
            )
            or data.get('type') in ('cassandra', 'scylla')
            and bundle_path is not None
        ):
            fs_store = FsStore()
            integrations_dir = Config()['paths']['integrations']
            folder_name = f'integration_files_{integration_record.company_id}_{integration_record.id}'
            integration_dir = os.path.join(integrations_dir, folder_name)
            fs_store.get(
                folder_name,
                integration_dir,
                integrations_dir
            )

        if not sensitive_info:
            if 'password' in data:
                data['password'] = None
            if (
                data.get('type') == 'redis'
                and isinstance(data.get('connection'), dict)
                and 'password' in data['connection']
            ):
                data['connection'] = None

        data['id'] = integration_record.id
        data['name'] = integration_record.name

        return data

    def get_by_id(self, id, company_id=None, sensitive_info=True):
        integration_record = session.query(Integration).filter_by(company_id=company_id, id=id).first()
        return self._get_integration_record_data(integration_record, sensitive_info)

    def get(self, name, company_id=None, sensitive_info=True, case_sensitive=False):
        if case_sensitive:
            integration_record = session.query(Integration).filter_by(company_id=company_id, name=name).first()
        else:
            integration_record = session.query(Integration).filter(
                (Integration.company_id == company_id)
                & (func.lower(Integration.name) == func.lower(name))
            ).first()
        return self._get_integration_record_data(integration_record, sensitive_info)

    def get_all(self, company_id=None, sensitive_info=True):
        integration_records = session.query(Integration).filter_by(company_id=company_id).all()
        integration_dict = {}
        for record in integration_records:
            if record is None or record.data is None:
                continue
            integration_dict[record.name] = self._get_integration_record_data(record, sensitive_info)
        return integration_dict

    def check_connections(self):
        connections = {}
        for integration_name, integration_meta in self.get_all().items():
            handler = self.create_handler(
                handler_type=integration_meta.get('type'),
                connection_data=integration_meta
            )
            status = handler.check_connection()
            connections[integration_name] = status.get('success', False)
        return connections

    def create_handler(self, name: str = None, handler_type: str = None,
                       connection_data: dict = {}, company_id: int = None):
        fs_store = FsStore()

        handler_ars = dict(
            name=name,
            db_store=None,
            fs_store=fs_store,
            connection_data=connection_data
        )

        if handler_type == 'files':
            handler_ars['file_controller'] = WithKWArgsWrapper(
                FileController(),
                company_id=company_id
            )
        elif handler_type == 'views':
            handler_ars['view_controller'] = WithKWArgsWrapper(
                ViewController(),
                company_id=company_id
            )

        return self.handler_modules[handler_type].Handler(**handler_ars)

    def get_handler(self, name, company_id=None, case_sensitive=False):
        if name.lower() == 'files':
            integration_data = {
                'type': 'files',
                'name': 'files'
            }
        elif name.lower() == 'views':
            integration_data = {
                'type': 'views',
                'name': 'views'
            }
        else:
            if case_sensitive:
                integration_record = session.query(Integration).filter_by(company_id=company_id, name=name).first()
            else:
                integration_record = session.query(Integration).filter(
                    (Integration.company_id == company_id)
                    & (func.lower(Integration.name) == func.lower(name))
                ).first()
            integration_data = self._get_integration_record_data(integration_record, True)

        integration_type = integration_data.get('type')
        integration_name = integration_data.get('name')
        del integration_data['name']

        if integration_type not in self.handler_modules:
            raise Exception(f"Cant find handler for '{integration_name}' ({integration_type})")

        handler = self.create_handler(
            name=integration_name,
            handler_type=integration_type,
            connection_data=integration_data,
            company_id=company_id
        )

        return handler

    def _load_handler_modules(self):
        mindsdb_path = Path(importlib.util.find_spec('mindsdb').origin).parent
        handlers_path = mindsdb_path.joinpath('integrations/handlers')
        self.handler_modules = {}
        self.handlers_import_status = {}
        for hanlder_dir in handlers_path.iterdir():
            if hanlder_dir.is_dir() is False:
                continue
            handler_folder_name = str(hanlder_dir.name)
            dependencies = []
            requirements_txt = hanlder_dir.joinpath('requirements.txt')
            if requirements_txt.is_file():
                with open(str(requirements_txt), 'rt') as f:
                    dependencies = [x.strip(' \t') for x in f.readlines()]
                    dependencies = [x for x in dependencies if len(x) > 0]
            try:
                handler_module = importlib.import_module(f'mindsdb.integrations.handlers.{handler_folder_name}')
                self.handler_modules[handler_module.Handler.type] = handler_module
                self.handlers_import_status[handler_folder_name] = {
                    'success': True,
                    'dependencies': dependencies
                }
            except Exception as e:
                self.handlers_import_status[handler_folder_name] = {
                    'success': False,
                    'error_message': str(e),
                    'dependencies': dependencies
                }

    def get_handlers_import_status(self):
        return self.handlers_import_status
