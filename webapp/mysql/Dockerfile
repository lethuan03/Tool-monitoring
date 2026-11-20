# MySQL với script tạo user 'exporter' đã đóng gói sẵn — dùng cho chế độ
# triển khai Docker Hub + EC2 user-data (xem README mục 12). KHÔNG cần
# bind-mount ./mysql/init từ host nữa: EC2 chỉ cần pull image này.
FROM mysql:8.0.40
COPY init/ /docker-entrypoint-initdb.d/
RUN chmod +x /docker-entrypoint-initdb.d/*.sh
