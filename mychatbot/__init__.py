import pymysql

# Django 5 requires mysqlclient 2.2.1+; PyMySQL reports 1.4.6 by default.
# Same shim MY-SHOP uses so both projects share one database driver stack.
pymysql.version_info = (2, 2, 1, "final", 0)

pymysql.install_as_MySQLdb()
