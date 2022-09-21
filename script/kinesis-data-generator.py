# Copyright 2021 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

import sys
import random
from faker import Faker
from datetime import datetime, timedelta
import mysql.connector
from mysql.connector import errorcode
from awsglue.utils import getResolvedOptions


fake = Faker()

params = [
    'db_host',
    'db_port',
    'db_user',
    'db_password',
    'db_database',
    'mode'
]
args = getResolvedOptions(sys.argv, params)

cnx = mysql.connector.connect(
    host=args['db_host'],
    port=args['db_port'],
    user=args['db_user'],
    password=args['db_password'],
    database=args['db_database']
)
cur = cnx.cursor()


def create_table(cursor):
    event_table_description = (
        "CREATE TABLE `sport_event` ("
        "  `event_id` INT NOT NULL AUTO_INCREMENT,"
        "  `sport_type` VARCHAR(256) NOT NULL,"
        "  `start_date` DATETIME NOT NULL,"
        "  `location` VARCHAR(256) NOT NULL,"
        "  PRIMARY KEY (`event_id`)"
        ") ENGINE=InnoDB"
    )
    try:
        print("Creating table {}: ".format("sport_event"), end='')
        cursor.execute(event_table_description)
    except mysql.connector.Error as err:
        if err.errno == errorcode.ER_TABLE_EXISTS_ERROR:
            print("already exists.")
        else:
            print(err.msg)
    else:
        print("OK")

    ticket_table_description = (
        "CREATE TABLE `ticket` ("
        "  `ticket_id` INT NOT NULL AUTO_INCREMENT,"
        "  `event_id` INT NOT NULL,"
        "  `seat_level` VARCHAR(256) NOT NULL,"
        "  `seat_location` VARCHAR(256) NOT NULL,"
        "  `ticket_price` INT NOT NULL,"
        "  PRIMARY KEY (`ticket_id`),"
        "  FOREIGN KEY (`event_id`) REFERENCES sport_event(`event_id`)"
        ") ENGINE=InnoDB"
    )
    try:
        print("Creating table {}: ".format("ticket"), end='')
        cursor.execute(ticket_table_description)
    except mysql.connector.Error as err:
        if err.errno == errorcode.ER_TABLE_EXISTS_ERROR:
            print("already exists.")
        else:
            print(err.msg)
    else:
        print("OK")


    customer_table_description = (
        "CREATE TABLE `customer` ("
        "  `customer_id` INT NOT NULL AUTO_INCREMENT,"
        "  `customer_name` VARCHAR(256) NOT NULL,"
        "  `email_address` VARCHAR(256) NOT NULL,"
        "  `phone_number` VARCHAR(256) NOT NULL,"
        "  PRIMARY KEY (`customer_id`)"
        ") ENGINE=InnoDB"
    )
    try:
        print("Creating table {}: ".format("customer"), end='')
        cursor.execute(customer_table_description)
    except mysql.connector.Error as err:
        if err.errno == errorcode.ER_TABLE_EXISTS_ERROR:
            print("already exists.")
        else:
            print(err.msg)
    else:
        print("OK")


    ticket_activity_table_description = (
        "CREATE TABLE `ticket_activity` ("
        "  `ticket_id` INT NOT NULL,"
        "  `purchased_by` INT NOT NULL,"
        "  `created_at` DATETIME NOT NULL,"
        "  `updated_at` DATETIME NOT NULL,"
        "  PRIMARY KEY (`ticket_id`),"
        "  FOREIGN KEY (`ticket_id`) REFERENCES ticket(`ticket_id`),"
        "  FOREIGN KEY (`purchased_by`) REFERENCES customer(`customer_id`)"
        ") ENGINE=InnoDB"
    )
    try:
        print("Creating table {}: ".format("ticket_activity"), end='')
        cursor.execute(ticket_activity_table_description)
    except mysql.connector.Error as err:
        if err.errno == errorcode.ER_TABLE_EXISTS_ERROR:
            print("already exists.")
        else:
            print(err.msg)
    else:
        print("OK")

sport_type_list = [
    "Baseball",
    "Football"
]

location_list = [
    "Seattle, US",
    "New York, US",
    "San Francisco, US",
    "Los Angeles, US",
    "Boston, US",
    "Chicago, US"
]


def generate_seat_list(num):
    seat_level_price = {
        "Standard": 100,
        "Premium": 300
    }
    list = []
    for seat_level in seat_level_price.keys():
        for i in range(num):
            list.append((seat_level, f"{seat_level[0]}-{i}", seat_level_price[seat_level]))
    return list


def initialize_data(cursor, connection):
    today = datetime.today()

    try:
        print("Initializing data in table `sport_event` and `ticket`:", end='')
        add_event = ("INSERT INTO sport_event "
                     "(sport_type, start_date, location) "
                     "VALUES (%s, %s, %s)")
        for num in range(50):
            future_date = (today + timedelta(days=random.randint(30,365))).date()
            data_event = (random.choice(sport_type_list), future_date, random.choice(location_list))
            cursor.execute(add_event, data_event)
            latest_event_id = cursor.lastrowid

            add_ticket = ("INSERT INTO ticket "
                          "(event_id, seat_level, seat_location, ticket_price) "
                          "VALUES (%s, %s, %s, %s)")
            for seat_level, seat_location, ticket_price in generate_seat_list(100):
                data_ticket = (latest_event_id, seat_level, seat_location, ticket_price)
                cursor.execute(add_ticket, data_ticket)
    except mysql.connector.Error as err:
        print(err.msg)
    else:
        print("OK")

    try:
        print("Ingesting data in table `customer`:", end='')
        add_customer = ("INSERT INTO customer "
                        "(customer_name, email_address, phone_number) "
                        "VALUES (%s, %s, %s)")

        for num in range(1000):
            data_customer = (fake.name(), fake.ascii_email(), fake.phone_number())
            cursor.execute(add_customer, data_customer)
    except mysql.connector.Error as err:
        print(err.msg)
    else:
        print("OK")

    connection.commit()


def ingest_data(cursor, connection):
    today = datetime.today()

    print("Selecting one record from table {}".format("ticket"))
    cursor.execute("SELECT ticket.ticket_id FROM ticket LEFT OUTER JOIN ticket_activity on ticket.ticket_id = ticket_activity.ticket_id WHERE ticket_activity.ticket_id is NULL ORDER BY RAND() LIMIT 1")
    rows = cursor.fetchall()
    ticket_id = 0
    for row in rows:
        ticket_id = row[0]

    print("Selecting one record from table {}".format("customer"))
    cursor.execute("SELECT customer_id FROM customer ORDER BY RAND() LIMIT 1")
    rows = cursor.fetchall()
    customer_id = 0
    for row in rows:
        customer_id = row[0]

    print("Ingesting data into table {}: ".format("ticket_activity"), end='')
    add_ticket_activity = ("INSERT INTO ticket_activity "
                 "(ticket_id, purchased_by, created_at, updated_at) "
                 "VALUES (%s, %s, %s, %s)")

    data_ticket_activity = (ticket_id, customer_id, today, today)
    cursor.execute(add_ticket_activity, data_ticket_activity)

    connection.commit()


if args['mode'] == 'initial':
    create_table(cur)
    initialize_data(cur, cnx)

ingest_data(cur, cnx)

cur.close()
cnx.close()
