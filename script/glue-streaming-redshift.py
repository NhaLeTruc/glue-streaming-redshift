# Copyright 2021 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

import sys
import boto3
from botocore.exceptions import WaiterError
from botocore.waiter import WaiterModel
from botocore.waiter import create_waiter_with_client
from awsglue.utils import getResolvedOptions
from pyspark.context import SparkContext
from pyspark.sql.functions import col, desc, rank, regexp_replace, sha2
from pyspark.sql.window import Window
from awsglue.context import GlueContext
from awsglue.job import Job

from awsglue.dynamicframe import DynamicFrame

params = [
    'JOB_NAME',
    'TempDir',
    'src_glue_database_name',
    'src_glue_table_name',
    'rds_connection_name',
    'rds_db_endpoint',
    'src_rds_table_name_sport_event',
    'src_rds_table_name_ticket',
    'src_rds_table_name_customer',
    'dst_redshift_database_name',
    'dst_redshift_schema_name',
    'dst_redshift_table_name',
    'dst_redshift_db_user',
    'dst_redshift_cluster_identifier',
    'primary_keys',
    'redshift_connection_name'
]
args = getResolvedOptions(sys.argv, params)
src_glue_database_name = args["src_glue_database_name"]
src_glue_table_name = args["src_glue_table_name"]
rds_connection_name = args["rds_connection_name"]
rds_db_endpoint = args["rds_db_endpoint"]
src_rds_table_name_sport_event = args["src_rds_table_name_sport_event"]
src_rds_table_name_ticket = args["src_rds_table_name_ticket"]
src_rds_table_name_customer = args["src_rds_table_name_customer"]
dst_redshift_database_name = args["dst_redshift_database_name"]
dst_redshift_schema_name = args["dst_redshift_schema_name"]
dst_redshift_table_name = args["dst_redshift_table_name"]
dst_redshift_db_user = args["dst_redshift_db_user"]
dst_redshift_cluster_identifier = args["dst_redshift_cluster_identifier"]
primary_keys = [x.strip() for x in args['primary_keys'].split(',')]
redshift_connection_name = args["redshift_connection_name"]
stg_table_name = dst_redshift_table_name + "_stage"

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
jdbc_conf = glue_context.extract_jdbc_conf(connection_name=rds_connection_name)

# Create DynamicFrames from dimension tables on RDS
df_sport_event = spark.read.format("jdbc") \
    .option("url", rds_db_endpoint)\
    .option("driver", "com.mysql.jdbc.Driver")\
    .option("dbtable", src_rds_table_name_sport_event)\
    .option("user", jdbc_conf['user'])\
    .option("password", jdbc_conf['password']).load()
df_sport_event = df_sport_event.select(
    col('event_id').cast("int"),
    col('sport_type'),
    col('start_date'),
    col('location')
)
df_sport_event.printSchema()
df_sport_event.show()

df_ticket = spark.read.format("jdbc") \
    .option("url", rds_db_endpoint) \
    .option("driver", "com.mysql.jdbc.Driver") \
    .option("dbtable", src_rds_table_name_ticket) \
    .option("user", jdbc_conf['user']) \
    .option("password", jdbc_conf['password']).load()
df_ticket = df_ticket.select(
    col('ticket_id').cast("int"),
    col('event_id').alias('ticket_event_id').cast("int"),
    col('seat_level'),
    col('seat_location'),
    col('ticket_price')
)
df_ticket.printSchema()
df_ticket.show()

df_customer = spark.read.format("jdbc") \
    .option("url", rds_db_endpoint) \
    .option("driver", "com.mysql.jdbc.Driver") \
    .option("dbtable", src_rds_table_name_customer) \
    .option("user", jdbc_conf['user']) \
    .option("password", jdbc_conf['password']).load()
df_customer = df_customer.select(
    col('customer_id').cast('int'),
    col('customer_name'),
    col('email_address'),
    col('phone_number')
)
df_customer.printSchema()
df_customer.show()

df_ticket_sport_event = df_ticket.join(df_sport_event, df_ticket.ticket_event_id == df_sport_event.event_id, 'inner')
df_ticket_sport_event.printSchema()
df_ticket_sport_event.show()


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


def processBatch(data_frame, batchId):
    if data_frame.count() > 0:
        data_frame.printSchema()
        data_frame.show()

        # Filter CDC records with only INSERT or UPDATE
        df_insert_update_only = data_frame.select(col('data.*'), col('metadata.*')) \
            .filter(col('record-type') == 'data') \
            .filter((col('operation') == 'insert') | (col('operation') == 'update'))
        df_insert_update_only.show()

        window = Window.partitionBy(primary_keys).orderBy(desc('timestamp'))
        df_to_be_staged = df_insert_update_only.withColumn('rnk', rank().over(window)) \
            .filter(col('rnk')==1) \
            .select(
                col('ticket_id').alias("activity_ticket_id").cast("int"),
                col('purchased_by').cast("int"),
                col('created_at'),
                col('updated_at')
            )
        df_to_be_staged.show()

        # Denormalize data by joining DataFrame with dimension tables
        df_denormalized = df_to_be_staged.join(
            df_ticket_sport_event, df_to_be_staged.activity_ticket_id == df_ticket_sport_event.ticket_id, "inner"
        )
        df_denormalized = df_denormalized.join(
            df_customer, df_denormalized.purchased_by == df_customer.customer_id, "inner"
        )

        # Mask/Tokenize data
        df_enriched = df_denormalized.withColumn('phone_number', regexp_replace('phone_number', r'(\d)', '*')) \
            .withColumn('email_address', sha2('email_address', 256))
        df_enriched = df_enriched.select(
            col('activity_ticket_id').alias("ticket_id").cast("int"),
            col('ticket_event_id').alias("event_id").cast("int"),
            col('sport_type'),
            col('start_date'),
            col('location'),
            col('seat_level'),
            col('seat_location'),
            col('ticket_price'),
            col('purchased_by').cast("int"),
            col('customer_name'),
            col('email_address'),
            col('phone_number'),
            col('created_at'),
            col('updated_at')
        )
        dynamic_frame = DynamicFrame.fromDF(df_enriched, glue_context, "from_data_frame")
        dynamic_frame.toDF().printSchema()
        dynamic_frame.toDF().show()

        # Pre query for staging table. Using Redshift Data API instead of preactions in order to avoid invalid reference.
        pre_query = f"""
        create table if not exists {dst_redshift_schema_name}.{dst_redshift_table_name} (
            ticket_id INT NOT NULL,
            event_id INT NOT NULL,
            sport_type VARCHAR(MAX) NOT NULL,
            start_date TIMESTAMP NOT NULL,
            location VARCHAR(MAX) NOT NULL,
            seat_level VARCHAR(MAX) NOT NULL,
            seat_location VARCHAR(MAX) NOT NULL,
            ticket_price INT NOT NULL,
            purchased_by INT NOT NULL,
            customer_name VARCHAR(MAX) NOT NULL,
            email_address VARCHAR(MAX) NOT NULL,
            phone_number VARCHAR(MAX) NOT NULL,
            created_at TIMESTAMP NOT NULL,
            updated_at TIMESTAMP NOT NULL,
            PRIMARY KEY (ticket_id)
        );
        drop table if exists {dst_redshift_schema_name}.{stg_table_name};
        create table {dst_redshift_schema_name}.{stg_table_name} 
            as select * from {dst_redshift_schema_name}.{dst_redshift_table_name} where 1=2;
        """
        runQuery(pre_query)

        # Post query for staging
        condition_expression = ""
        for i, primary_key in enumerate(primary_keys):
            if i == 0:
                condition_expression = condition_expression + f"{dst_redshift_schema_name}.{stg_table_name}.{primary_key} = {dst_redshift_schema_name}.{dst_redshift_table_name}.{primary_key}"
            else:
                condition_expression = condition_expression + " and " + f"{dst_redshift_schema_name}.{stg_table_name}.{primary_key} = {dst_redshift_schema_name}.{dst_redshift_table_name}.{primary_key}"
        post_query = f"""
            delete from {dst_redshift_schema_name}.{dst_redshift_table_name} 
                using {dst_redshift_schema_name}.{stg_table_name} 
                where {condition_expression}; 
            insert into {dst_redshift_schema_name}.{dst_redshift_table_name} 
                select 
                    ticket_id,
                    event_id,
                    sport_type,
                    start_date,
                    location,
                    seat_level,
                    seat_location,
                    ticket_price,
                    purchased_by,
                    customer_name,
                    email_address,
                    phone_number, 
                    created_at, 
                    updated_at 
                from {dst_redshift_schema_name}.{stg_table_name} where ticket_id is not NULL; 
            drop table {dst_redshift_schema_name}.{stg_table_name}
        """

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


# Read from the DataFrame coming via Kinesis, and run processBatch method for batches in every 100 seconds
glue_context.forEachBatch(
    frame=data_frame_kinesis,
    batch_function=processBatch,
    options={
        "windowSize": "100 seconds",
        "checkpointLocation": f"{args['TempDir']}/checkpoint/{args['JOB_NAME']}/"
    }
)
job.commit()
