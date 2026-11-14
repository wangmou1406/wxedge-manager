FROM python:3.12-slim
WORKDIR /opt/wxedge-manager
COPY app/ /opt/wxedge-manager/app/
EXPOSE 8080
CMD ["python3", "/opt/wxedge-manager/app/main.py", "--data-dir", "/var/lib/wxedge-manager"]
