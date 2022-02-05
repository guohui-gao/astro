import logging
import unittest.mock

import pytest
from airflow.exceptions import AirflowException
from airflow.models import DAG, DagRun
from airflow.models import TaskInstance as TI
from airflow.utils import timezone
from airflow.utils.session import create_session

# Import Operator
import astro.sql as aql
from astro.sql.table import Table
from tests.operators import utils as test_utils

log = logging.getLogger(__name__)
DEFAULT_DATE = timezone.datetime(2016, 1, 1)
import os

dir_path = os.path.dirname(os.path.realpath(__file__))


class TestSQLParsing(unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.dag = DAG(
            "test_dag",
            default_args={
                "owner": "airflow",
                "start_date": DEFAULT_DATE,
            },
        )

    def tearDown(self):
        super().tearDown()
        with create_session() as session:
            session.query(DagRun).delete()
            session.query(TI).delete()

    def test_parse(self):
        with self.dag:
            rendered_tasks = aql.render(dir_path + "/passing_dag")

        assert (
            rendered_tasks.get("agg_orders")
            and rendered_tasks.get("agg_orders").operator.parameters == {}
        )
        assert rendered_tasks.get("join_customers_and_orders")
        join_params = rendered_tasks.get(
            "join_customers_and_orders"
        ).operator.parameters
        assert len(join_params) == 2
        assert (
            join_params["customers_table"].operator.task_id == "render.customers_table"
        )

    def test_parse_missing_table(self):
        with pytest.raises(AirflowException):
            with self.dag:
                rendered_tasks = aql.render(dir_path + "/missing_table_dag")

    def test_parse_missing_table_with_inputs(self):
        with self.dag:
            rendered_tasks = aql.render(
                dir_path + "/missing_table_dag",
                agg_orders=Table("foo"),
                customers_table=Table("customers_table"),
            )

    def test_parse_missing_table_with_input_and_upstream(self):
        with self.dag:
            agg_orders = aql.load_file("s3://foo")
            rendered_tasks = aql.render(
                dir_path + "/missing_table_dag",
                agg_orders=agg_orders,
                customers_table=Table("customers_table"),
            )

    def test_parse_frontmatter(self):
        with self.dag:
            rendered_tasks = aql.render(dir_path + "/front_matter_dag")
        customers_table_task = rendered_tasks.get("customers_table")
        assert customers_table_task
        assert customers_table_task.operator.database == "foo"
        assert customers_table_task.operator.schema == "bar"

        customer_output_table = customers_table_task.operator.output_table
        assert customer_output_table.table_name == "my_table"
        assert customer_output_table.schema == "my_schema"

        new_customers_table = rendered_tasks.get("get_new_customers")
        new_customer_output_table = new_customers_table.operator.output_table
        assert new_customer_output_table.table_name == ""
        assert new_customer_output_table.schema == None
        assert new_customer_output_table.database == "my_db"
        assert new_customer_output_table.conn_id == "my_conn_id"

        assert (
            new_customers_table.operator.sql
            == "SELECT * FROM {customers_table} WHERE member_since > DATEADD(day, -7, '{{ execution_date }}')"
        )

    def test_parse_creates_xcom(self):
        """
        Runs two tasks with a direct dependency, the DAG will fail if task two can not inherit the table produced by task 1
        :return:
        """
        with self.dag:
            rendered_tasks = aql.render(dir_path + "/single_task_dag")

        test_utils.run_dag(self.dag)

    def test_parse_to_dataframe(self):
        """
        Runs two tasks with a direct dependency, the DAG will fail if task two can not inherit the table produced by task 1
        :return:
        """
        import pandas as pd

        from astro.dataframe import dataframe as adf

        @adf
        def dataframe_func(df: pd.DataFrame):
            print(df.to_string)

        with self.dag:
            rendered_tasks = aql.render(dir_path + "/postgres_simple_tasks")
            dataframe_func(rendered_tasks["test_inheritance"])

        test_utils.run_dag(self.dag)
