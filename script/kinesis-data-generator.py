import sys
import json
import time
import boto3
from pyspark.sql.window import Window
from pyspark.sql.functions import row_number, lit
from awsglue.utils import getResolvedOptions
from pyspark.context import SparkContext
from awsglue.context import GlueContext
from awsglue.job import Job

params = [
    'JOB_NAME',
    'snk_data_stream',
    'snk_region',    
    'src_glue_database_name',
    'src_glue_table_name',
    'messages_per_sec'
]
args = getResolvedOptions(sys.argv, params)

sc = SparkContext()
glue_context = GlueContext(sc)
spark = glue_context.spark_session
job = Job(glue_context)
job.init(args['JOB_NAME'], args)

stream_name = args['snk_data_stream']

send_num = int(args['messages_per_sec'])

kinesis_client = boto3.client('kinesis', region_name=args['snk_region'])

src_df = glue_context.create_data_frame.from_catalog(
    database = args['src_glue_database_name'],
    table_name = args['src_glue_table_name'],    
    transformation_ctx = "src_df"
)

w = Window().orderBy(lit('a'))
src_df = src_df.withColumn("row_num", row_number().over(w))
src_df_len = int(src_df.count())

def put_records(df, num):
    try:
        for x in range(1,src_df_len,num):
            records = df.filter(df.row_num.between(x,x+num-1)).select()
            messages = records.rdd.map(lambda row: row.asDict()).collect()
            response = kinesis_client.put_records(
                    StreamName=stream_name,
                    Records= json.dumps(messages)
                )
            time.sleep(2)
            
    except:
        print("Error:", sys.exc_info()[0])
        raise
    else:
        return response

put_records(src_df, send_num)