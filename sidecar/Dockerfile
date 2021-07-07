FROM python:3.7-buster

LABEL maintainer="nickrus@cisco.com,stmcnutt@cisco.com"

COPY ./app /app

WORKDIR /app

# set environment variables
ENV PYTHONDONTWRITEBYTECODE 1
ENV PYTHONUNBUFFERED 1

# install dependencies
# COPY ./requirements.txt .
RUN pip3 install -r requirements.txt
