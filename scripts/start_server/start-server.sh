#! /bin/bash

# Start replication server locally, and include logging (requires replication_version=0.0.21a15)
clear
replication.server -p 5555 -pwd admin -t 1000 -l DEBUG -lf server.log