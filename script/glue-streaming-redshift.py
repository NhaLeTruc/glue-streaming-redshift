import sys
import boto3
import pyspark.sql.functions as F
from botocore.exceptions import WaiterError
from botocore.waiter import WaiterModel
from botocore.waiter import create_waiter_with_client
from awsglue.utils import getResolvedOptions
from pyspark.context import SparkContext
from pyspark.sql.window import Window
from pyspark.sql import DataFrame
from awsglue.context import GlueContext
from awsglue.job import Job
from functools import reduce

from awsglue.dynamicframe import DynamicFrame

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

# Create Spark DataFrame from the source Kinesis table
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
    except WaiterError as e:
        print (e)


def cleanDeltas(df,key):
    pass


def processBatch(data_frame, batchId):
    if data_frame.count() > 0:
        # TODO: Filter records to 4 categories: INSERT; UPDATE; DELETE; TRADE base on non-empty id fields.
        # INSERT; UPDATE; DELETE (0 price, 0 qty) are easy inserts
        # TRADE turn trade_quantity to negative then insert.
        # Reduce L3 data from 16 columns to 8, see below
        
        columns_names = ["seq_num","order_id","side","price","quantity","delta_time","delta_type"]

        df_add = data_frame.na.drop(subset=["add_order_id"])
        null_counts = df_add.select([F.count(F.when(F.col(c).isNull(), c)).alias(c) for c in df_add.columns]).collect()[0].asDict()
        to_drop = [k for k, v in null_counts.items() if v > 0]
        df_add.drop(*to_drop)
        df_add.withColumn('delta_type', F.lit("ADD"))
        df_add.toDF(*columns_names)

        df_update = data_frame.na.drop(subset=["update_order_id"])
        null_counts = df_update.select([F.count(F.when(F.col(c).isNull(), c)).alias(c) for c in df_update.columns]).collect()[0].asDict()
        to_drop = [k for k, v in null_counts.items() if v > 0]
        df_update.drop(*to_drop)
        df_update.withColumn('delta_type', F.lit("UPDATE"))
        df_update.toDF(*columns_names)

        df_trade = data_frame.na.drop(subset=["trade_order_id"])
        null_counts = df_trade.select([F.count(F.when(F.col(c).isNull(), c)).alias(c) for c in df_trade.columns]).collect()[0].asDict()
        to_drop = [k for k, v in null_counts.items() if v > 0]
        df_trade.drop(*to_drop)
        df_trade.withColumn('delta_type', F.lit("TRADE"))
        df_trade.toDF(*columns_names)

        df_delete = data_frame.na.drop(subset=["delete_order_id"])
        null_counts = df_delete.select([F.count(F.when(F.col(c).isNull(), c)).alias(c) for c in df_delete.columns]).collect()[0].asDict()
        to_drop = [k for k, v in null_counts.items() if v > 0]
        df_delete.drop(*to_drop)
        df_delete.withColumnRenamed("delete_order_id","order_id")
        df_delete.withColumnRenamed("delete_side","side")
        df_delete.withColumnRenamed("time","delta_time")
        df_delete.withColumn('price', F.lit(0))
        df_delete.withColumn('quantity', F.lit(0))
        df_delete.withColumn('delta_type', F.lit("DELETE"))    

        dfs = [df_add, df_update, df_trade, df_delete]
        df_to_be_staged = reduce(DataFrame.unionByName, dfs)


        # Pre query for staging table. Using Redshift Data API instead of preactions in order to avoid invalid reference.
        pre_query = f"""
        CREATE TABLE IF NOT EXISTS {dst_redshift_schema_name}.{deltas_redshift_table_name} (
            seq_num INT8 NOT NULL,
            order_id INT8 NOT NULL,
            side VARCHAR(4) NOT NULL,
            price FLOAT NOT NULL,
            quantity INT NOT NULL,
            delta_time TIMESTAMP NOT NULL,
            delta_type VARCHAR(6) NOT NULL
            PRIMARY KEY (seq_num)
        );

        DROP TABLE IF EXISTS {dst_redshift_schema_name}.{stg_table_name};

        CREATE TABLE {dst_redshift_schema_name}.{stg_table_name} 
            AS SELECT * FROM {dst_redshift_schema_name}.{deltas_redshift_table_name} WHERE 1=2;
        """
        runQuery(pre_query)

        # Post query for staging

        post_query = f"""
            DELETE FROM {dst_redshift_schema_name}.{deltas_redshift_table_name} 
                USING {dst_redshift_schema_name}.{stg_table_name} 
                WHERE {dst_redshift_schema_name}.{deltas_redshift_table_name}.seq_num = {dst_redshift_schema_name}.{stg_table_name}.seq_num
            ; 

            INSERT INTO {dst_redshift_schema_name}.{deltas_redshift_table_name} 
                SELECT 
                    seq_num,
                    order_id,
                    side,
                    price,
                    quantity,
                    delta_time,
                    delta_type
                FROM {dst_redshift_schema_name}.{stg_table_name} WHERE seq_num IS NOT NULL
            ;                        

            DROP TABLE {dst_redshift_schema_name}.{stg_table_name}
            ;
        """

        datasink = glue_context.write_dynamic_frame.from_jdbc_conf(
            frame=df_to_be_staged,
            catalog_connection=redshift_connection_name,
            connection_options={
                "database": dst_redshift_database_name,
                "dbtable": f"{dst_redshift_schema_name}.{stg_table_name}",
                "postactions": post_query
            },
            redshift_tmp_dir=args['TempDir'],
            transformation_ctx="write_redshift"
        )


# Read from the DataFrame coming via Kinesis, and run processBatch method for batches in every 30 seconds
glue_context.forEachBatch(
    frame=data_frame_kinesis,
    batch_function=processBatch,
    options={
        "windowSize": "5 seconds",
        "checkpointLocation": f"{args['TempDir']}/checkpoint/{args['JOB_NAME']}/"
    }
)
job.commit()
