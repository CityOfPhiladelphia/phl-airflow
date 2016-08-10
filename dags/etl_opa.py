"""
ETL Pipeline for OPA Data

Connections:
- phl-s3-etl (must have credentials set in a JSON object in the Extra field,
  i.e.: {"aws_access_key_id":"...", "aws_secret_access_key":"..."})
"""

from airflow import DAG
from airflow.operators import BashOperator
from airflow.operators import PythonOperator
from airflow.operators import DatumCSV2TableOperator
from airflow.operators import CleanupOperator
from airflow.operators import FileDownloadOperator
from airflow.operators import SlackNotificationOperator
from datetime import datetime, timedelta

# ============================================================
# Defaults - these arguments apply to all operators

default_args = {
    'owner': 'airflow',  # TODO: Look up what owner is
    'depends_on_past': False,  # TODO: Look up what depends_on_past is
    'retries': 0,
    'retry_delay': timedelta(minutes=5),
    'start_date': datetime(2017, 1, 1, 0, 0, 0),
    'on_failure_callback': SlackNotificationOperator.failed(),
    # 'queue': 'bash_queue',  # TODO: Lookup what queue is
    # 'pool': 'backfill',  # TODO: Lookup what pool is
}

pipeline = DAG('etl_opa_v2', default_args=default_args)  # TODO: Look up how to schedule a DAG

# ------------------------------------------------------------
# Extract - copy files to the staging area

def mkdir():
    import tempfile
    return tempfile.mkdtemp()

mk_staging = PythonOperator(
    task_id='staging',
    dag=pipeline,

    python_callable=mkdir,
)

extract_a = FileDownloadOperator(
    task_id='download_properties',
    dag=pipeline,

    source_type='sftp',
    source_conn_id='phl-ftp-etl',
    source_path='/OPA_Property_CD/\'br63trf.os13sd\'',

    dest_path='{{ ti.xcom_pull("staging") }}/br63trf.os13sd',
)

extract_b = FileDownloadOperator(
    task_id='download_building_codes',
    dag=pipeline,

    source_type='sftp',
    source_conn_id='phl-ftp-etl',
    source_path='/OPA_Property_CD/\'br63trf.buildcod\'',

    dest_path='{{ ti.xcom_pull("staging") }}/br63trf.buildcod',
)

extract_c = FileDownloadOperator(
    task_id='download_street_codes',
    dag=pipeline,

    source_type='sftp',
    source_conn_id='phl-ftp-etl',
    source_path='/OPA_Property_CD/\'br63trf.stcode\'',

    dest_path='{{ ti.xcom_pull("staging") }}/br63trf.stcode',
)

extract_d = FileDownloadOperator(
    task_id='download_off_property',
    dag=pipeline,

    source_type='sftp',
    source_conn_id='phl-ftp-etl',
    source_path='/OPA_Property_CD/\'br63trf.offpr\'',

    dest_path='{{ ti.xcom_pull("staging") }}/br63trf.offpr',
)

extract_e = FileDownloadOperator(
    task_id='download_assessment_history',
    dag=pipeline,

    source_type='sftp',
    source_conn_id='phl-ftp-etl',
    source_path='/OPA_Property_CD/\'br63trf.nicrt4wb\'',

    dest_path='{{ ti.xcom_pull("staging") }}/br63trf.nicrt4wb',
)

# ------------------------------------------------------------
# Transform - run each table through a cleanup script

transform_a = BashOperator(
    task_id='clean_properties',
    dag=pipeline,

    bash_command=
        'cat {{ ti.xcom_pull("staging") }}/br63trf.os13sd | '
        'phl-properties > {{ ti.xcom_pull("staging") }}/properties.csv',
)

transform_b = BashOperator(
    task_id='clean_building_codes',
    dag=pipeline,

    bash_command=
        'cat {{ ti.xcom_pull("staging") }}/br63trf.buildcod | '
        'phl-building-codes > {{ ti.xcom_pull("staging") }}/building_codes.csv',
)

transform_c = BashOperator(
    task_id='clean_street_codes',
    dag=pipeline,

    bash_command=
        'cat {{ ti.xcom_pull("staging") }}/br63trf.buildcod | '
        'phl-street-codes > {{ ti.xcom_pull("staging") }}/building_codes.csv',
)

transform_d = BashOperator(
    task_id='clean_off_property',
    dag=pipeline,

    bash_command=
        'cat {{ ti.xcom_pull("staging") }}/br63trf.offpr | '
        'phl-off-property > {{ ti.xcom_pull("staging") }}/off_property.csv',
)

transform_e = BashOperator(
    task_id='clean_assessment_history',
    dag=pipeline,

    bash_command=
        'cat {{ ti.xcom_pull("staging") }}/br63trf.nicrt4wb | '
        'phl-assessment-history > {{ ti.xcom_pull("staging") }}/assessment_history.csv',
)


# ------------------------------------------------------------
# Load - copy tables into on-prem database(s)

load_a = DatumCSV2TableOperator(
    task_id='load_properties',
    dag=pipeline,

    csv_path='{{ ti.xcom_pull("staging") }}/properties.csv',
    db_conn_id='phl-warehouse-staging',
    db_table_name='opa_properties',
)

load_b = DatumCSV2TableOperator(
    task_id='load_building_codes',
    dag=pipeline,

    csv_path='{{ ti.xcom_pull("staging") }}/building_codes.csv',
    db_conn_id='phl-warehouse-staging',
    db_table_name='opa_building_codes',
)

load_c = DatumCSV2TableOperator(
    task_id='load_street_codes',
    dag=pipeline,

    csv_path='{{ ti.xcom_pull("staging") }}/street_codes.csv',
    db_conn_id='phl-warehouse-staging',
    db_table_name='opa_street_codes',
)

load_d = DatumCSV2TableOperator(
    task_id='load_off_property',
    dag=pipeline,

    csv_path='{{ ti.xcom_pull("staging") }}/off_property.csv',
    db_conn_id='phl-warehouse-staging',
    db_table_name='opa_off_property',
)

load_e = DatumCSV2TableOperator(
    task_id='load_assessment_history',
    dag=pipeline,

    csv_path='{{ ti.xcom_pull("staging") }}/assessment_history.csv',
    db_conn_id='phl-warehouse-staging',
    db_table_name='opa_assessment_history',
)

# ------------------------------------------------------------
# Postscript - clean up the staging area

cleanup = CleanupOperator(
    task_id='cleanup_staging',
    dag=pipeline,
    paths='{{ ti.xcom_pull("staging") }}',
)


# ============================================================
# Configure the pipeline's dag

mk_staging >> extract_a >> transform_a >> load_a >> cleanup
mk_staging >> extract_b >> transform_b >> load_b >> cleanup
mk_staging >> extract_c >> transform_c >> load_c >> cleanup
mk_staging >> extract_d >> transform_d >> load_d >> cleanup
mk_staging >> extract_e >> transform_e >> load_e >> cleanup
