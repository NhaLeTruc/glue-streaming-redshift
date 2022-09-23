Guide:
    + Configure your working enviroment for AWS CLI . Please follow this instruction: https://docs.aws.amazon.com/cli/latest/userguide/cli-chap-getting-started.html
    + Open your command shell in folder which your unzipped folder 'grasshopper-de-test'. Replace in the 2nd command: 'YOURBUCKETNAME' & 'YOURSTACKNAME' with a unique name; and 'YOURPASSWORD' with a unique password; then run the commands below: 
        cd grasshopper-de-test
        aws cloudformation create-stack --stack-name YOURSTACKNAME --template-body file://streaming-infra.yaml --capabilities CAPABILITY_NAMED_IAM --parameters ParameterKey=S3BucketName,ParameterValue=YOURBUCKETNAME ParameterKey=DatabaseUserPassword,ParameterValue=YOURPASSWORD ParameterKey=SubnetAzA,ParameterValue=ap-southeast-1a ParameterKey=SubnetAzB,ParameterValue=ap-southeast-1b

    + Log into your AWS account, search bar is on top left. Search 'CloudFormation'. Click on first service appears with same name.
    + Once in CloudFormation console UI, top left below search bar there is 'Stacks'. Click on 'Stack'.
    + There should be a stack named 'YOURSTACKNAME-orders'. Click on it bring you to your stack info page. In the middle of your UI, there should be a 'Resources' tab. Click on it.
    + Wait for your stack's status to turn 'CREATE_COMPLETE' then continue.
    + Search 'AWS Glue Studio' in your UI searchbar. It should appear in the 'Features' section of the result. Open in new tab. You will need CloudFormation later.
    + In AWS Glue Studio console, click on 'Jobs' in top left. There shold be two jobs in your studio:
        1. streaming-kinesis2redshift-YOURSTACKNAME
        2. data-generator-YOURSTACKNAME
    + Run 'data-generator-YOURSTACKNAME' first then 'streaming-kinesis2redshift-YOURSTACKNAME'. There is a 'Run Job' button on top right of 'Your jobs' tab.
    + Wait two minute then back to your CloudFormation browser tab.
    + CTRL + F then paste in 'AWS::Redshift::Cluster', and you should see a resource of the type. Click on its 'Physical ID'. A new tab should open to your cluster.
    + Click on Cluster name 'book-YOURSTACKNAME'. Top right is a orange dropdown button 'Query data'. Choose 'Query in query editor'.
    + Top right is a orange button 'Connect to database'. If it do not automatically log in or log in a different cluster than 'book-YOURSTACKNAME'; database: 'dev'; schema: 'public'. Then you need to fill in the details: 
        1. Cluster: 'book-YOURSTACKNAME'
        2. Database name: 'dev'
        3. Database user: 'dbmaster'
    + Once connected, you should see a table named 'deltas' with all orders' deltas transformed and stored.