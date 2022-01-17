"""
Copyright Astronomer, Inc.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""
"""
Unittest module to test Agnostic Load File function.

Requires the unittest, pytest, and requests-mock Python libraries.

Run test:
    AIRFLOW__ASTRO__CONN_AWS_DEFAULT=aws://AKIAZG42HVH6Z3B6ELRB:SgwfrcO2NdKpeKhUG77K%2F6B2HuRJJopbHPV84NbY@ \
    python3 -m unittest tests.operators.test_agnostic_load_file.TestAgnosticLoadFile.test_aql_local_file_to_postgres

"""

import logging
import os
import pathlib
import unittest.mock

import pandas as pd
import pytest
import utils as test_utils
from airflow.exceptions import DuplicateTaskIdFound
from airflow.models import DAG, DagRun
from airflow.models import TaskInstance as TI
from airflow.providers.postgres.hooks.postgres import PostgresHook
from airflow.providers.snowflake.hooks.snowflake import SnowflakeHook
from airflow.utils import timezone
from airflow.utils.session import create_session
from airflow.utils.state import State
from airflow.utils.types import DagRunType
from google.api_core.exceptions import NotFound
from google.cloud import storage

# Import Operator
from astro.sql.operators.agnostic_load_file import AgnosticLoadFile, load_file
from astro.sql.operators.temp_hooks import TempPostgresHook
from astro.sql.table import Table, TempTable

log = logging.getLogger(__name__)
DEFAULT_DATE = timezone.datetime(2016, 1, 1)
OUTPUT_TABLE_NAME = test_utils.get_table_name("load_file_test_table")
OUTPUT_SCHEMA = os.getenv("SNOWFLAKE_SCHEMA")
CWD = pathlib.Path(__file__).parent


def drop_table_postgres(table_name, postgres_conn):
    cursor = postgres_conn.cursor()
    cursor.execute(f"DROP TABLE IF EXISTS {table_name} CASCADE;")
    postgres_conn.commit()
    cursor.close()
    postgres_conn.close()


class TestAgnosticLoadFile(unittest.TestCase):
    """
    Test agnostic load file.
    """

    gcs_creds_filename = "gcp_credentials.json"
    bucket_name = "dag-authoring"
    blob_file_name = "homes.csv"
    storage_client = None

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.SNOWFLAKE_OUTPUT_TABLE_NAME = test_utils.get_table_name(
            "expected_table_from_csv"
        )

    @classmethod
    def tearDownClass(cls) -> None:
        test_utils.drop_table_snowflake(
            table_name=cls.SNOWFLAKE_OUTPUT_TABLE_NAME,  # type: ignore
            database=os.getenv("SNOWFLAKE_DATABASE"),  # type: ignore
            schema=os.getenv("SNOWFLAKE_SCHEMA"),  # type: ignore
            warehouse=os.getenv("SNOWFLAKE_WAREHOUSE"),  # type: ignore
            conn_id="snowflake_conn",
        )

    def setUp(self):
        super().setUp()
        self.clear_run()
        self.addCleanup(self.clear_run)
        self.dag = DAG(
            "test_dag", default_args={"owner": "airflow", "start_date": DEFAULT_DATE}
        )
        self.init_storage_client()

    def setup_gcs_credentials_for_astro(self):
        path = str(CWD) + "/" + self.gcs_creds_filename
        if os.path.isfile(path):
            self.delete_gcs_creds()

        content = os.environ["GCP_CREDENTIALS"]
        with open(path, "w") as f:
            f.write(content)
        os.environ["AIRFLOW__ASTRO__GOOGLE_APPLICATION_CREDENTIALS"] = path
        return path

    def init_storage_client(self):
        service_account_credentials = os.getenv("GCP_CREDENTIALS")
        if service_account_credentials:
            path = self.setup_gcs_credentials_for_astro()
            self.storage_client = storage.Client.from_service_account_json(path)
        else:
            self.storage_client = storage.Client()

    def upload_blob(self):
        self.delete_blob()

        with open(str(CWD) + "/../data/" + str(self.blob_file_name)) as f:
            content = f.read()

        bucket = self.storage_client.bucket(self.bucket_name)
        blob = bucket.blob(self.blob_file_name)
        t = blob.upload_from_filename(str(CWD) + "/../data/" + str(self.blob_file_name))
        print("File uploaded.")

    def delete_blob(self):
        bucket = self.storage_client.bucket(self.bucket_name)
        blob = bucket.blob(self.blob_file_name)
        try:
            blob.delete()
            print("Blob {} deleted.".format(self.blob_file_name))
        except NotFound as e:
            print("File {} not found.".format(self.blob_file_name))

    def delete_gcs_creds(self):
        os.remove(str(CWD) + "/" + self.gcs_creds_filename)

    def clear_run(self):
        self.run = False

    def tearDown(self):
        super().tearDown()
        with create_session() as session:
            session.query(DagRun).delete()
            session.query(TI).delete()

    def create_and_run_task(self, decorator_func, op_args, op_kwargs):
        with self.dag:
            f = decorator_func(*op_args, **op_kwargs)

        dr = self.dag.create_dagrun(
            run_id=DagRunType.MANUAL.value,
            start_date=timezone.utcnow(),
            execution_date=DEFAULT_DATE,
            data_interval=[DEFAULT_DATE, DEFAULT_DATE],
            state=State.RUNNING,
        )
        f.operator.run(start_date=DEFAULT_DATE, end_date=DEFAULT_DATE)
        return f

    def create_and_run_tasks(self, decorator_funcs):
        tasks = []
        with self.dag:
            for decorator_func in decorator_funcs:
                tasks.append(
                    decorator_func["func"](
                        *decorator_func["op_args"], **decorator_func["op_kwargs"]
                    )
                )

        dr = self.dag.create_dagrun(
            run_id=DagRunType.MANUAL.value,
            start_date=timezone.utcnow(),
            execution_date=DEFAULT_DATE,
            state=State.RUNNING,
        )
        for task in tasks:
            task.operator.run(start_date=DEFAULT_DATE, end_date=DEFAULT_DATE)
        return tasks

    def test_path_validation(self):
        test_table = [
            {"input": "S3://mybucket/puppy.jpg", "output": True},
            {
                "input": "https://my-bucket.s3.us-west-2.amazonaws.com/puppy.png",
                "output": True,
            },
            {"input": "/etc/someFile/randomFileName.csv", "output": False},
            {"input": "\x00", "output": False},
            {"input": "a" * 256, "output": False},
        ]

        for test in test_table:
            assert AgnosticLoadFile.validate_path(test["input"]) == test["output"]

    def test_poc_for_need_unique_task_id_for_same_path(self):
        OUTPUT_TABLE_NAME = "expected_table_from_csv_1"

        tasks_params = []
        for _ in range(5):
            tasks_params.append(
                {
                    "func": load_file,
                    "op_args": (),
                    "op_kwargs": {
                        "path": str(CWD) + "/../data/homes.csv",
                        "file_conn_id": "",
                        "task_id": "task_id",
                        "output_table": Table(
                            OUTPUT_TABLE_NAME,
                            database="pagila",
                            conn_id="postgres_conn",
                        ),
                    },
                }
            )
        tasks_params[-1]["op_kwargs"]["task_id"] = "task_id"
        try:
            self.create_and_run_tasks(tasks_params)
            assert False
        except DuplicateTaskIdFound:
            assert True

    def test_unique_task_id_for_same_path(self):
        OUTPUT_TABLE_NAME = "expected_table_from_csv_1"

        tasks_params = []
        for _ in range(4):
            tasks_params.append(
                {
                    "func": load_file,
                    "op_args": (),
                    "op_kwargs": {
                        "path": str(CWD) + "/../data/homes.csv",
                        "file_conn_id": "",
                        "output_table": Table(
                            OUTPUT_TABLE_NAME,
                            database="pagila",
                            conn_id="postgres_conn",
                        ),
                    },
                }
            )
        tasks_params[-1]["op_kwargs"]["task_id"] = "task_id"

        tasks = self.create_and_run_tasks(tasks_params)

        assert tasks[0].operator.task_id != tasks[1].operator.task_id
        assert tasks[1].operator.task_id == "load_file_homes_csv__1"
        assert tasks[2].operator.task_id == "load_file_homes_csv__2"
        assert tasks[3].operator.task_id == "task_id"

    def test_aql_local_file_to_postgres_no_table_name(self):
        OUTPUT_TABLE_NAME = "expected_table_from_csv"

        self.hook_target = TempPostgresHook(
            postgres_conn_id="postgres_conn", schema="pagila"
        )

        # Drop target table
        drop_table_postgres(OUTPUT_TABLE_NAME, self.hook_target.get_conn())

        task = self.create_and_run_task(
            load_file,
            (),
            {
                "path": str(CWD) + "/../data/homes.csv",
                "file_conn_id": "",
                "output_table": TempTable(database="pagila", conn_id="postgres_conn"),
            },
        )

        # Read table from db
        df = pd.read_sql(
            f"SELECT * FROM tmp_astro.test_dag_load_file_homes_csv_1",
            con=self.hook_target.get_conn(),
        )

        assert df.iloc[0].to_dict() == {
            "sell": 142.0,
            "list": 160.0,
            "living": 28.0,
            "rooms": 10.0,
            "beds": 5.0,
            "baths": 3.0,
            "age": 60.0,
            "acres": 0.28,
            "taxes": 3167.0,
        }

    def test_aql_overwrite_existing_table(self):
        OUTPUT_TABLE_NAME = "expected_table_from_csv"

        self.hook_target = TempPostgresHook(
            postgres_conn_id="postgres_conn", schema="pagila"
        )

        # Drop target table
        drop_table_postgres(OUTPUT_TABLE_NAME, self.hook_target.get_conn())

        self.create_and_run_task(
            load_file,
            (),
            {
                "path": str(CWD) + "/../data/homes.csv",
                "file_conn_id": "",
                "output_table": Table(
                    table_name=OUTPUT_TABLE_NAME,
                    database="pagila",
                    conn_id="postgres_conn",
                ),
            },
        )

        with create_session() as session:
            session.query(DagRun).delete()
            session.query(TI).delete()

        self.create_and_run_task(
            load_file,
            (),
            {
                "path": str(CWD) + "/../data/homes.csv",
                "file_conn_id": "",
                "output_table": Table(
                    table_name=OUTPUT_TABLE_NAME,
                    database="pagila",
                    conn_id="postgres_conn",
                ),
            },
        )

    def test_aql_s3_file_to_postgres(self):
        OUTPUT_TABLE_NAME = "expected_table_from_s3_csv"

        self.hook_target = TempPostgresHook(
            postgres_conn_id="postgres_conn", schema="pagila"
        )

        # Drop target table
        drop_table_postgres(OUTPUT_TABLE_NAME, self.hook_target.get_conn())

        self.create_and_run_task(
            load_file,
            (),
            {
                "path": "s3://tmp9/homes.csv",
                "file_conn_id": "",
                "output_table": Table(
                    table_name=OUTPUT_TABLE_NAME,
                    database="pagila",
                    conn_id="postgres_conn",
                ),
            },
        )

        # Read table from db
        df = pd.read_sql(
            f"SELECT * FROM tmp_astro.{OUTPUT_TABLE_NAME}",
            con=self.hook_target.get_conn(),
        )

        assert df.iloc[0].to_dict()["Sell"] == 142.0

    def test_aql_s3_file_to_postgres_no_table_name(self):
        OUTPUT_TABLE_NAME = "test_dag_load_file_homes_csv_2"

        self.hook_target = TempPostgresHook(
            postgres_conn_id="postgres_conn", schema="pagila"
        )

        # Drop target table
        drop_table_postgres(
            f"tmp_astro.{OUTPUT_TABLE_NAME}", self.hook_target.get_conn()
        )

        self.create_and_run_task(
            load_file,
            (),
            {
                "path": "s3://tmp9/homes.csv",
                "file_conn_id": "",
                "output_table": Table(
                    table_name=OUTPUT_TABLE_NAME,
                    database="pagila",
                    conn_id="postgres_conn",
                ),
            },
        )

        # Read table from db
        df = pd.read_sql(
            f"SELECT * FROM tmp_astro.{OUTPUT_TABLE_NAME}",
            con=self.hook_target.get_conn(),
        )

        assert df.iloc[0].to_dict()["Sell"] == 142.0

    def test_aql_s3_file_to_postgres_specify_schema(self):
        OUTPUT_TABLE_NAME = "expected_table_from_s3_csv"

        self.hook_target = TempPostgresHook(
            postgres_conn_id="postgres_conn", schema="pagila"
        )

        # Drop target table
        drop_table_postgres(OUTPUT_TABLE_NAME, self.hook_target.get_conn())

        self.create_and_run_task(
            load_file,
            (),
            {
                "path": "s3://tmp9/homes.csv",
                "file_conn_id": "",
                "output_table": Table(
                    OUTPUT_TABLE_NAME,
                    database="pagila",
                    conn_id="postgres_conn",
                    schema="public",
                ),
            },
        )

        # Read table from db
        df = pd.read_sql(
            f"SELECT * FROM {OUTPUT_TABLE_NAME}", con=self.hook_target.get_conn()
        )

        assert df.iloc[0].to_dict()["Sell"] == 142.0

    def test_aql_gcs_file_to_postgres(self):
        # To Do: add service account creds
        self.upload_blob()
        OUTPUT_TABLE_NAME = "expected_table_from_gcs_csv"

        self.hook_target = PostgresHook(
            postgres_conn_id="postgres_conn", schema="pagila"
        )

        # Drop target table
        drop_table_postgres(OUTPUT_TABLE_NAME, self.hook_target.get_conn())

        self.create_and_run_task(
            load_file,
            (),
            {
                "path": "gs://dag-authoring/homes.csv",
                "file_conn_id": "",
                "output_table": Table(
                    OUTPUT_TABLE_NAME,
                    database="pagila",
                    conn_id="postgres_conn",
                    schema="public",
                ),
            },
        )

        # Read table from db
        df = pd.read_sql(
            f"SELECT * FROM {OUTPUT_TABLE_NAME}", con=self.hook_target.get_conn()
        )
        assert df.iloc[0].to_dict()["sell"] == 142.0


@pytest.fixture
def sample_dag():
    yield DAG("test_dag", default_args={"owner": "airflow", "start_date": DEFAULT_DATE})
    with create_session() as session:
        session.query(DagRun).delete()
        session.query(TI).delete()


def create_and_run_task(dag, decorator_func, op_args, op_kwargs):
    with dag:
        function = decorator_func(*op_args, **op_kwargs)

    _ = dag.create_dagrun(
        run_id=DagRunType.MANUAL.value,
        start_date=timezone.utcnow(),
        data_interval=[DEFAULT_DATE, DEFAULT_DATE],
        execution_date=DEFAULT_DATE,
        state=State.RUNNING,
    )
    function.operator.run(start_date=DEFAULT_DATE, end_date=DEFAULT_DATE)
    return function


@pytest.fixture
def hook(request):
    if request.param == "snowflake":
        hook = SnowflakeHook(
            snowflake_conn_id="snowflake_conn",
            schema=os.getenv("SNOWFLAKE_SCHEMA"),
            database=os.getenv("SNOWFLAKE_DATABASE"),
            warehouse=os.getenv("SNOWFLAKE_WAREHOUSE"),
        )
        hook.run(
            f"DROP TABLE IF EXISTS {os.getenv('SNOWFLAKE_SCHEMA')}.{OUTPUT_TABLE_NAME}"
        )
        yield hook
        hook.run(
            f"DROP TABLE IF EXISTS {os.getenv('SNOWFLAKE_SCHEMA')}.{OUTPUT_TABLE_NAME}"
        )
    elif request.param == "postgres":
        POSTGRES_TABLE_NAME = "test_dag_load_file_homes_csv_2"
        hook = TempPostgresHook(postgres_conn_id="postgres_conn", schema="pagila")
        hook.run(f"DROP TABLE IF EXISTS tmp_astro.{OUTPUT_TABLE_NAME}")
        yield hook
        hook.run(f"DROP TABLE IF EXISTS tmp_astro.{OUTPUT_TABLE_NAME}")
    else:
        raise ValueError(f"Unsupported database {request.param}")


@pytest.mark.parametrize("hook", ["snowflake", "postgres"], indirect=True)
@pytest.mark.parametrize("file_type", ["ndjson", "json", "csv", "parquet"])
def test_load_file(sample_dag, hook, file_type):
    if isinstance(hook, TempPostgresHook):
        params = {
            "path": str(CWD) + f"/../data/sample.{file_type}",
            "file_conn_id": "",
            "output_table": Table(
                table_name=OUTPUT_TABLE_NAME,
                database="pagila",
                conn_id="postgres_conn",
            ),
        }
        schema = "tmp_astro"
    else:
        params = {
            "path": str(CWD) + f"/../data/sample.{file_type}",
            "file_conn_id": "",
            "output_table": Table(
                table_name=OUTPUT_TABLE_NAME,
                database=os.getenv("SNOWFLAKE_DATABASE"),
                schema=OUTPUT_SCHEMA,
                warehouse=os.getenv("SNOWFLAKE_WAREHOUSE"),
                conn_id="snowflake_conn",
            ),
        }
        schema = OUTPUT_SCHEMA

    create_and_run_task(sample_dag, load_file, (), params)

    # Read table from db
    df = hook.get_pandas_df(f"SELECT * FROM {schema}.{OUTPUT_TABLE_NAME}")
    assert len(df) == 3
    expected = pd.DataFrame(
        [
            {"id": 1, "name": "First"},
            {"id": 2, "name": "Second"},
            {"id": 3, "name": "Third with unicode पांचाल"},
        ]
    )
    assert df.rename(columns=str.lower).equals(expected)
