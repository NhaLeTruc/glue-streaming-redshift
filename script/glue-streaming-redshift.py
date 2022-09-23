import sys
import boto3
from functools import reduce
from botocore.waiter import WaiterModel
from botocore.waiter import create_waiter_with_client
from awsglue.utils import getResolvedOptions
from awsglue.dynamicframe import DynamicFrame
from awsglue.context import GlueContext
from awsglue.job import Job
from pyspark.context import SparkContext
from pyspark.sql import DataFrame
from pyspark.sql.functions import col, length, lit

params = [
    'JOB_NAME',
    'TempDir',
    'src_glue_database_name',
    'src_glue_table_name',    
    'dst_redshift_database_name',
    'dst_redshift_schema_name',
    'deltas_redshift_table_name',
    'bbo_redshift_view_name',
    'dst_redshift_db_user',
    'dst_redshift_cluster_identifier',
    'redshift_connection_name'
]
args = getResolvedOptions(sys.argv, params)
src_glue_database_name = args["src_glue_database_name"]
src_glue_table_name = args["src_glue_table_name"]
dst_redshift_database_name = args["dst_redshift_database_name"]
dst_redshift_schema_name = args["dst_redshift_schema_name"]
deltas_redshift_table_name = args["deltas_redshift_table_name"]
bbo_redshift_view_name = args["bbo_redshift_view_name"]
dst_redshift_db_user = args["dst_redshift_db_user"]
dst_redshift_cluster_identifier = args["dst_redshift_cluster_identifier"]
redshift_connection_name = args["redshift_connection_name"]
stg_table_name = deltas_redshift_table_name + "_stage"

sc = SparkContext()
glue_context = GlueContext(sc)
spark = glue_context.spark_session
job = Job(glue_context)
job.init(args['JOB_NAME'], args)

redshift_data = boto3.client('redshift-data')
# custom waiter for Redshift Data API
waiter_config = {
    'version': 2,
    'waiters': {
        'DataAPIExecution': {
            'operation': 'DescribeStatement',
            'delay': 2,
            'maxAttempts': 10,
            'acceptors': [
                {
                    "matcher": "path",
                    "expected": "FINISHED",
                    "argument": "Status",
                    "state": "success"
                },
                {
                    "matcher": "pathAny",
                    "expected": ["PICKED","STARTED","SUBMITTED"],
                    "argument": "Status",
                    "state": "retry"
                },
                {
                    "matcher": "pathAny",
                    "expected": ["FAILED","ABORTED"],
                    "argument": "Status",
                    "state": "failure"
                }
            ],
        },
    },
}
waiter_name = "DataAPIExecution"
waiter_model = WaiterModel(waiter_config)
custom_waiter = create_waiter_with_client(waiter_name, waiter_model, redshift_data)

data_frame_kinesis = glue_context.create_data_frame.from_catalog(
    database=src_glue_database_name,
    table_name=src_glue_table_name,
    transformation_ctx="data_frame_kinesis",
    additional_options={
        "startingPosition": "TRIM_HORIZON",
        "inferSchema": "false"
    }
)

def runQuery(query_string):
    query_result = redshift_data.execute_statement(
        ClusterIdentifier=dst_redshift_cluster_identifier,
        Database=dst_redshift_database_name,
        DbUser=dst_redshift_db_user,
        Sql=query_string,
    )
    query_id = query_result['Id']
    try:
        print(f"Running query: {query_string}")
        custom_waiter.wait(Id=query_id)
    except:
        print("Error:", sys.exc_info())
        raise

cname = ["order_id","side","price","qty"]
columns_names = ["seq_num","order_id","side","price","quantity","delta_time","delta_type"]

def cleanDeltas(df ,key : str):
    df = df.filter(length(df[key + '_order_id']) > 10)
    print(key + ': ' + str(df.count()))

    df_cname = ["seq_num"] + [key + '_' + a for a in cname]  + ["time"]
    to_drop = list(set(df.columns) - set(df_cname))
    df = df.drop(*to_drop)
    df = df.withColumn('delta_type', lit(str(key)))
    df = df.toDF(*columns_names)    
    return df


def processBatch(data_frame, batchId):
    if data_frame.count() > 0:
        df_add = cleanDeltas(data_frame, 'add')
        df_update = cleanDeltas(data_frame, 'update')
        df_trade = cleanDeltas(data_frame, 'trade')

        df_delete = data_frame.filter(length(data_frame['delete_order_id']) > 10)
        to_drop = list(set(df_delete.columns) - set(["delete_order_id","delete_side","seq_num","time"]))
        df_delete = df_delete.drop(*to_drop)
        print('delete: ' + str(df_delete.count()))
        df_delete = df_delete.withColumnRenamed("delete_order_id","order_id")
        df_delete = df_delete.withColumnRenamed("delete_side","side")
        df_delete = df_delete.withColumnRenamed("time","delta_time")
        df_delete = df_delete.withColumn('price', lit(0))
        df_delete = df_delete.withColumn('quantity', lit(0))
        df_delete = df_delete.withColumn('delta_type', lit("delete"))    

        dfs = [df_add, df_update, df_trade, df_delete]
        df_to_be_staged = reduce(DataFrame.unionByName, dfs)
        print(df_to_be_staged.head(10))
        df_to_be_staged = df_to_be_staged.select(
            col('seq_num').cast("string"),
            col('order_id').cast("string"),
            col('side').cast("string"),
            col('price').cast("string"),
            col('quantity').cast("string"),
            col('delta_time').cast("string"),
            col('delta_type').cast("string")
        )

        dynamic_frame = DynamicFrame.fromDF(df_to_be_staged, glue_context, "from_data_frame")

        # Pre query for staging table. Using Redshift Data API instead of preactions in order to avoid invalid reference.
        pre_query = f"""
        CREATE TABLE IF NOT EXISTS {dst_redshift_schema_name}.{deltas_redshift_table_name} (
            seq_num VARCHAR(MAX) NOT NULL,
            order_id VARCHAR(MAX) NOT NULL,
            side VARCHAR(4) NOT NULL,
            price VARCHAR(10) NOT NULL,
            quantity VARCHAR(10) NOT NULL,
            delta_time VARCHAR(30) NOT NULL,
            delta_type VARCHAR(6) NOT NULL,
            PRIMARY KEY (seq_num)
        );
        DROP TABLE IF EXISTS {dst_redshift_schema_name}.{stg_table_name};
        CREATE TABLE {dst_redshift_schema_name}.{stg_table_name} 
            AS SELECT * FROM {dst_redshift_schema_name}.{deltas_redshift_table_name} WHERE 1=2;
        """
        runQuery(pre_query.strip())

        # Post query for staging
        post_query = f"""
            DELETE FROM {dst_redshift_schema_name}.{deltas_redshift_table_name} 
                USING {dst_redshift_schema_name}.{stg_table_name} 
                WHERE {dst_redshift_schema_name}.{deltas_redshift_table_name}.seq_num = {dst_redshift_schema_name}.{stg_table_name}.seq_num;
            INSERT INTO {dst_redshift_schema_name}.{deltas_redshift_table_name} 
                SELECT 
                    seq_num,
                    order_id,
                    side,
                    price,
                    quantity,
                    delta_time,
                    delta_type
                FROM {dst_redshift_schema_name}.{stg_table_name} WHERE seq_num IS NOT NULL;
            DROP TABLE {dst_redshift_schema_name}.{stg_table_name};
        """
        post_query = post_query.strip()

        datasink = glue_context.write_dynamic_frame.from_jdbc_conf(
            frame=dynamic_frame,
            catalog_connection=redshift_connection_name,
            connection_options={
                "database": dst_redshift_database_name,
                "dbtable": f"{dst_redshift_schema_name}.{stg_table_name}",
                "postactions": post_query
            },
            redshift_tmp_dir=args['TempDir'],
            transformation_ctx="write_redshift"
        )

glue_context.forEachBatch(
    frame=data_frame_kinesis,
    batch_function=processBatch,
    options={
        "windowSize": "5 seconds",
        "checkpointLocation": f"{args['TempDir']}/checkpoint/{args['JOB_NAME']}/"
    }
)
job.commit()