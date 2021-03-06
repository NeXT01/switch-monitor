# encoding: utf-8

# TODO：严重的BUG：在尝试使用uwsgi部署的时候，执行到扫描线程的pick = pickle.dumps(switch)时会卡住，原因未知
'''
# TODO：
流量持续过大报警
学校E152B系统版本更新（旧版本存在BUG）
完成E152B适配（重启功能）
BUG：交换机刚重启时端口流量爆表
前端：
    首页图表大小自适应屏幕大小（手机、1080p、4K……）
    加favicon
    导航栏显示优化
    端口流量单位自适应
    增加是否自动更新的复选框
    页面锚点刷新/跨页面跳转失效的BUG
配置页面功能：
    设置页面加参数：TCPING_TIMEOUT SCAN_THREADS SCAN_PROCESS SEND_MSG_DELAY CPU_THRESHOLD MEM_THRESHOLD  TEMP_THRESHOLD  DATA_RECORD_SAVED_DAYS  SCAN_REBOOT_HOURS
    设置页面加修改密码功能
    加交换机自动重启开关
'''
import sqlite3
import threading
import platform
import pickle
import os
import psutil
from multiprocessing import Process, Manager, cpu_count, Queue
from flask import *
from mod_ping import *
from mod_reboot_switch import *
from mod_snmp import *
from mod_weixin import *

from Config import USE_HTTPS, ADMIN_USERNAME, ADMIN_PASSWORD, WEB_USERNAME, WEB_PASSWORD, WEB_PORT, SEND_MSG_DELAY, \
    WEIXIN_STAT_TIME_H, WEIXIN_STAT_TIME_M, SW_REBOOT_TIME_H, SW_REBOOT_TIME_M, CPU_THRESHOLD, MEM_THRESHOLD, \
    TEMP_THRESHOLD, IF_SPEED_THRESHOLD, DATA_RECORD_SAVED_DAYS, SCAN_THREADS, SCAN_PROCESS, SCAN_REBOOT_HOURS, SNMP_MODE

global switches, buildings_list, switch_ping_num, switch_snmp_num, scan_processes, ip_queue, recive_queue, write_queue

# 定义数据库锁
lock_data = threading.Lock()  # data.db
lock_data_history = threading.Lock()  # data_history.db
lock_flow_history = threading.Lock()  # flow_history.db

# 定义进程间全局变量
Global = Manager().Namespace()
Global.reboot = False

# 定义队列
ip_queue = Queue()  # 任务队列
recive_queue = Queue()  # 任务结果提交队列
write_queue = Queue()  # 数据库写入队列


def start_switch_monitor():
    # !!!!!!!!!!!!!!!!!!!!这是主线程!!!!!!!!!!!!!!!!!!!!
    global switches, buildings_list, scan_processes

    print("\n")
    print("*" * 50)
    print("当前系统：", platform.system(), platform.architecture()[0], platform.machine())
    print("当前运行平台：", platform.platform())
    print("当前Python版本：", platform.python_version())
    print("CPU核心数：", cpu_count())
    print("扫描模式：", SNMP_MODE)
    print("扫描进程数：", SCAN_PROCESS)
    print("单进程线程数：", SCAN_THREADS)
    print("总扫描线程数：", SCAN_PROCESS * SCAN_THREADS)
    print("*" * 50)

    # 初始化微信接入
    refresh_token()  # 刷新微信token

    # 从文件读取交换机列表 TODO:直接从数据库读取交换机列表，用户可以上传csv或网页设置来修改交换机列表
    with open('switches_list.csv', mode='r', encoding='utf-8') as f:
        f.readline()  # 第一行是标题，我们不需要
        switches_list = f.read()
    switches_list = switches_list.strip().split("\n")  # 每行是一台交换机，包含IP、型号、楼栋、描述

    # 初始化交换机数据
    switches = []  # 用来存放交换机对象的列表
    buildings_list = []  # 用来存放楼栋名称的列表
    conn = sqlite3.connect("data.db")
    cursor = conn.cursor()
    tmp_time = time.time()
    cursor.execute('PRAGMA synchronous = OFF')  # 初始化时关闭写同步提高速度 http://www.runoob.com/sqlite/sqlite-pragma.html
    try:
        # 检查数据库有没有switches这个表，这个表用于存放交换机的IP、型号、楼栋、描述、掉线时间
        cursor.execute("select * from sqlite_master where type = 'table' and name = 'switches'")
        values = cursor.fetchall()
        if len(values) == 0:
            print("未发现数据表，开始创建数据表。")
            cursor.execute(
                '''
                CREATE TABLE switches
                (
                ip varchar(15),
                model varchar(10),
                building varchar(10),
                desc varchar(20),
                down_time int(10)
                )
               '''
            )
            print("数据表创建完成，初始化数据，大约需要1分钟……")
        else:
            print("发现数据表，读取数据……")
        # 读取数据（遍历所有交换机，检查是否在数据库，不在则添加，在则读取），创建交换机对象
        for a in range(0, len(switches_list)):
            info = switches_list[a].split(",")  # IP、型号、楼栋、描述、掉线时间
            cursor.execute("SELECT ip FROM switches WHERE ip='" + info[0] + "'")
            values = cursor.fetchall()
            if len(values) == 0:  # 交换机不存在则创建
                info.append('在线')
                cursor.execute(
                    "insert into switches values ('" + info[0] + "', '" + info[1] + "', '" + info[2] + "', '" + info[
                        3] + "', '" + info[4] + "')")
                conn.commit()
            else:  # 存在则读取其掉线时间
                cursor.execute("SELECT down_time FROM switches WHERE ip='" + info[0] + "'")
                conn.commit()
                info.append(cursor.fetchall()[0][0])
            # 创建交换机对象
            switches.append(Switch(a, info[0], info[1], info[2], info[3], info[4]))
            # 生成楼栋列表
            if info[2] not in buildings_list:
                buildings_list.append(info[2])
    finally:
        cursor.close()
        conn.close()
    print("初始化用时：", time.time() - tmp_time)

    # 检查历史记录数据库
    conn = sqlite3.connect("data_history.db")
    cursor = conn.cursor()
    tmp_time = time.time()
    cursor.execute('PRAGMA synchronous = OFF')  # 初始化时关闭写同步提高速度，无数据表时启动时间缩短为约1/3
    try:
        for switch in switches:
            cursor.execute("select * from sqlite_master where type = 'table' and name = '" + switch.ip + "'")
            values = cursor.fetchall()
            if len(values) == 0:  # 数据历史记录里没有此ip，新建一个
                cursor.execute(
                    "CREATE TABLE '" + switch.ip + "' (timestamp int(10),cpu char(5),mem char(5),temp char(5))")
    finally:
        conn.commit()
        cursor.close()
        conn.close()
    print("初始化历史记录数据库用时：", time.time() - tmp_time)

    # 初始化监控端口列表
    global port_list
    with open('port_list.csv', mode='r', encoding='utf-8') as f:
        f.readline()  # 第一行是标题，我们不需要
        port_list = f.read().strip().split("\n")  # 每行是一个端口，包含交换机IP、端口、描述

    # 检查流量速率记录数据库
    conn = sqlite3.connect("flow_history.db")
    cursor = conn.cursor()
    tmp_time = time.time()
    cursor.execute('PRAGMA synchronous = OFF')  # 初始化时关闭写同步提高速度
    try:
        for port in port_list:
            port_name = port[0:port.rfind(",")]
            cursor.execute(
                "select * from sqlite_master where type = 'table' and name = '" + port_name + "'")  # 检查有没有此端口的表
            values = cursor.fetchall()
            if len(values) == 0:
                # 没有此端口的表，新建一个
                cursor.execute(
                    "CREATE TABLE '" + port_name + "' (timestamp int(10),in_speed int(20),out_speed int(20))")
    finally:
        conn.commit()
        cursor.close()
        conn.close()
    print("初始化流量速率数据库用时：", time.time() - tmp_time)

    # 启动任务发放器
    threading.Thread(target=mission_distributer, name="线程_任务发放器", args=(ip_queue,)).start()

    # 启动扫描子进程
    scan_processes = []
    for a in range(0, SCAN_PROCESS):
        scan_processes.append(Process(target=scan_process, name="扫描进程" + str(a), args=(ip_queue, recive_queue,)))
        scan_processes[a].start()

    # 启动数据接收器
    threading.Thread(target=data_reciver, name="线程_数据接收器", args=(recive_queue, write_queue,)).start()

    # 启动数据监控器
    threading.Thread(target=data_supervisor, name="线程_数据监控器").start()

    # 启动数据记录器
    threading.Thread(target=data_history_recoder, name="线程_数据记录器", args=(write_queue,)).start()

    # 启动内存监视器（由于SNMP库存在内存泄漏，需要定时重启扫描进程。如果使用bin模式，则不需要）
    if SNMP_MODE == "lib":
        threading.Thread(target=memory_supervisior, name="线程_内存监视器", args=(ip_queue, recive_queue,)).start()

    # 完成
    print("初始化完成。监控程序已启动。")
    print("*" * 50)
    write_log("INFO: 监控启动")
    send_weixin_msg(time.strftime('[%Y-%m-%d %H:%M:%S] ', time.localtime()) + "\n""监控启动", 2)

    '''
    # 调试命令
    time.sleep(2)
    while 1:
        try:
            cmd = input("\033[1;35mDebug Command: \033[0m")  # print(switches)
            if cmd == 'exit':
                print("\033[1;36mExit debug.\033[0m\n")
                break
            if cmd == 'help': print("exit: exit debug.\n")
            exec(cmd)
        except:
            print('Input error.')
    '''


# 内存监视器（由于SNMP库存在内存泄漏，定时重启扫描进程）
def memory_supervisior(ip_queue, recive_queue):
    global scan_processes
    while 1:
        # 鉴于服务器上还运行了别的服务，这里不再根据内存使用率来重启进程，而是根据时间间隔
        time.sleep(60 * 60 * SCAN_REBOOT_HOURS)  # 每SCAN_REBOOT_HOURS小时重启一次
        write_log("达到重启时间，重启扫描进程。现在内存使用率为" + str(psutil.virtual_memory()[2]) + "%")
        Global.reboot = True
        for a in range(0, SCAN_PROCESS):
            scan_processes[a].join()
        Global.reboot = False
        scan_processes = []
        for a in range(0, SCAN_PROCESS):
            scan_processes.append(Process(target=scan_process, name="扫描进程" + str(a), args=(ip_queue, recive_queue,)))
            scan_processes[a].start()


class Switch(object):
    # 交换机对象
    def __init__(self, num, ip, model, building_belong, desc, down_time):  # IP、型号、楼栋、描述、掉线时间
        self.num = num
        self.ip = ip
        self.model = model
        self.building_belong = building_belong
        self.desc = desc
        self.down_time = down_time
        # 要获取的信息：CPU使用率、内存使用率、温度、启动时间、接口各种信息
        self.info_time = "等待获取"
        self.last_info_time = 0
        self.cpu_load = "等待获取"  # 监控重开时都显示等待获取
        self.mem_used = "等待获取"
        self.temp = "等待获取"
        self.up_time = "等待获取"
        self.name = "等待获取"
        self.if_status = []
        self.if_name = []
        self.if_descr = []
        self.if_uptime = []
        self.if_index = []
        self.if_ip = []
        self.if_ipindex = []
        self.if_ipmask = []
        self.if_in = []
        self.if_out = []
        self.if_in_speed = []
        self.if_out_speed = []
        self.if_speed = 0  # 端口带宽，单位Mbps，一般端口带宽都是固定的


def scan_process(ip_queue, recive_queue):
    t = []
    for a in range(0, SCAN_THREADS):
        t.append(threading.Thread(target=scan_switch, name="扫描线程" + str(a), args=(ip_queue, recive_queue,)))
        t[a].start()
    while not Global.reboot:
        time.sleep(10)
    for a in range(0, SCAN_THREADS):
        t[a].join()


def scan_switch(ip_queue, recive_queue):  # 扫描线程
    while not Global.reboot:
        # 获取一台交换机
        while ip_queue.empty():
            # write_log("任务队列空了！")
            time.sleep(0.2)
        # start_time = time.time()
        switch = pickle.loads(ip_queue.get())
        # print(switch.ip)
        # Ping获取在线情况
        if checkswitch(switch.ip) == True:
            if switch.down_time != "在线":
                switch.down_time = "在线"
                # write_db(switch.ip, "down_time", "在线")
        else:
            if switch.down_time == "在线":
                tmp_time = time.time()
                switch.down_time = tmp_time
                # write_db(switch.ip, "down_time", "%d" % tmp_time)
        # SNMP要获取的信息：CPU使用率、内存使用率、风扇、温度、启动时间、接口状态
        if switch.down_time == "在线":
            if switch != "等待获取":
                switch.last_info_time = switch.info_time
            switch.info_time = time.time()
            switch.up_time = SnmpWalk(switch.ip, switch.model, "up_time")
            if switch.up_time != "获取失败":  # 如果up_time能正确获取才获取其它信息。如果up_time不能正确获取，其它信息也不可能获取到。
                # 首先获取if_name，且只用获取一次
                if len(switch.if_name) == 0:  # 下面这些也只用获取一次
                    # TODO：在bin模式有小概率会丢包（SNMP基于UDP），这里仅处理了if_name这一项，其它未作处理。
                    switch.if_index = SnmpWalk(switch.ip, switch.model, "if_index")
                    switch.if_descr = SnmpWalk(switch.ip, switch.model, "if_descr")
                    switch.if_uptime = SnmpWalk(switch.ip, switch.model, "if_uptime")
                    switch.if_ip = SnmpWalk(switch.ip, switch.model, "if_ip")
                    switch.if_ipindex = SnmpWalk(switch.ip, switch.model, "if_ipindex")
                    switch.if_ipmask = SnmpWalk(switch.ip, switch.model, "if_ipmask")
                    switch.name = SnmpWalk(switch.ip, switch.model, "name")
                    for a in range(0, 5):
                        tmp_if_name = SnmpWalk(switch.ip, switch.model, "if_name")
                        if len(SnmpWalk(switch.ip, switch.model, "if_name")) == len(tmp_if_name):
                            switch.if_name = tmp_if_name
                            break
                # 获取其它数据
                if switch.cpu_load != "设备不支持":
                    switch.cpu_load = SnmpWalk(switch.ip, switch.model, "cpu_load")
                    switch.mem_used = SnmpWalk(switch.ip, switch.model, "mem_used")
                    switch.temp = SnmpWalk(switch.ip, switch.model, "temp")
                switch.if_status = SnmpWalk(switch.ip, switch.model, "if_status")
                switch.if_speed = SnmpWalk(switch.ip, switch.model, "if_speed")
                # 下面获取端口流过的字节数，从而计算端口实时流量
                last_if_in = switch.if_in
                last_if_out = switch.if_out
                switch.if_in = SnmpWalk(switch.ip, switch.model, "if_in")
                switch.if_out = SnmpWalk(switch.ip, switch.model, "if_out")
                if_in_speed = []
                if_out_speed = []
                # 下面这部分代码用于计算接口当前速率（实时流量）
                for a in range(0, len(switch.if_name)):
                    if len(last_if_in) != 0:  # 第一次获取时不进行速率计算
                        if last_if_in != '获取失败' and switch.if_in != '获取失败':  # 数据获取正常才进行计算
                            for b in range(0, 5):  # 有时候会获取不完整导致异常，这里检查数据是否完整，如不完整，重新获取
                                if len(switch.if_in) == len(switch.if_name): break
                                switch.if_in = SnmpWalk(switch.ip, switch.model, "if_in")
                            if len(switch.if_in) == len(switch.if_name):
                                try:
                                    if int(switch.if_in[a]) - int(last_if_in[a]) < 0:
                                        switch.if_in[a] = int(switch.if_in[a]) + 2 ** 64
                                    if_in_speed.append(int((int(switch.if_in[a]) - int(last_if_in[a])) / (
                                            int(switch.info_time) - int(switch.last_info_time))))
                                except:
                                    print("*" * 50)
                                    print(switch.ip, switch.if_in[a], last_if_in[a])
                                    print(switch.if_in, switch.if_in != '获取失败')
                                    print(last_if_in, last_if_in != '获取失败')
                                    print(last_if_in != '获取失败' and switch.if_in != '获取失败')
                                    print(len(switch.if_in), len(switch.if_name))
                                    print("*" * 50)
                            else:  # 如果数据不完整，直接改成获取失败
                                switch.if_in = '获取失败'
                            switch.if_in_speed = if_in_speed
                    if len(last_if_out) != 0:
                        if last_if_out != '获取失败' and switch.if_out != '获取失败':
                            for b in range(0, 5):
                                if len(switch.if_out) == len(switch.if_name): break
                                switch.if_out = SnmpWalk(switch.ip, switch.model, "if_out")
                            if len(switch.if_out) == len(switch.if_name):
                                try:
                                    if int(switch.if_out[a]) - int(last_if_out[a]) < 0:
                                        switch.if_out[a] = int(switch.if_out[a]) + 2 ** 64
                                    if_out_speed.append(int((int(switch.if_out[a]) - int(last_if_out[a])) / (
                                            int(switch.info_time) - int(switch.last_info_time))))
                                except:
                                    print("*" * 50)
                                    print(switch.ip, switch.if_out[a], last_if_out[a])
                                    print(switch.if_out, switch.if_out != '获取失败')
                                    print(last_if_out, last_if_out != '获取失败')
                                    print(last_if_out != '获取失败' and switch.if_out != '获取失败')
                                    print(len(switch.if_out), len(switch.if_name))
                                    print("*" * 50)
                            else:
                                switch.if_out = '获取失败'
                            switch.if_out_speed = if_out_speed
        # end_time = time.time()
        # print(switch.ip, switch.model, end_time - start_time)
        pick = pickle.dumps(switch)  # TODO: import时有BUG！！！！！！！！！！！！！线程会卡在这里
        # print(switch.ip, switch.model, end_time - start_time)
        recive_queue.put(pick)
        # print(switch.ip, switch.model, end_time - start_time)
        # SNMP平均消耗时间@树莓派3B：S2700 11秒 E152B：3.5秒 过载的E152B：50秒


def data_reciver(recive_queue, write_queue):  # 数据接收线程
    start_time = time.time()
    while 1:
        # print("接收队列长度：", recive_queue.qsize())
        if not recive_queue.empty():
            switch_datas = recive_queue.get()
            write_queue.put(switch_datas)  # 复制一份给写入队列（这样做好像损耗比较大，但是如果在接收线程写入数据库的话效率更低）
            switch = pickle.loads(switch_datas)
            try:
                if switch.num == len(switches) - 1 and not isinstance(switches[switch.num].info_time, str):
                    write_log("扫描一轮所需时间" + str(switch.info_time - switches[switch.num].info_time))  # BUG：有时候会0.0
                elif switch.num == len(switches) - 1 and isinstance(switches[switch.num].info_time, str):  # 第一轮的时间
                    if switch.info_time != "等待获取":
                        write_log("扫描一轮所需时间" + str(switch.info_time - start_time))
                switches[switch.num] = switch
            except:
                print("数据接收器报错，switch_datas=", switch_datas)
        else:
            time.sleep(1)


def data_history_recoder(write_queue):
    # 写入队列达到一定程度后把数据写入数据库
    # 代码还能优化下，现在不是很好看
    global port_list
    global lock_data_history
    global lock_flow_history
    switches_num = len(switches)
    one_time_num = switches_num // 3 + 1  # 队列阈值
    while (1):
        while write_queue.qsize() < one_time_num:
            time.sleep(1)
        # 申请锁，关闭写同步
        lock_data_history.acquire()
        conn = sqlite3.connect("data_history.db")
        cursor = conn.cursor()
        cursor.execute('PRAGMA synchronous = OFF')
        lock_flow_history.acquire()
        conn_flow = sqlite3.connect("flow_history.db")
        cursor_flow = conn_flow.cursor()
        cursor_flow.execute('PRAGMA synchronous = OFF')
        # 获取交换机数据
        _switches = []
        while not write_queue.empty():
            switch_datas = write_queue.get()
            _switches.append(pickle.loads(switch_datas))
        # tmp_time = time.time() # 用于计算写入数据库所用时间
        # 整点清理data_record_days*24小时前的记录。
        if time.localtime()[4] == 0:  # 分==0
            timestamp = str(int(time.time()) - DATA_RECORD_SAVED_DAYS * 24 * 60 * 60)
            for switch in _switches:
                cursor.execute("DELETE FROM '" + switch.ip + "' WHERE timestamp <= " + timestamp)
            for port in port_list:
                port_name = port[0:port.rfind(",")]
                cursor_flow.execute("DELETE FROM '" + port_name + "' WHERE timestamp <= " + timestamp)
        # 下面开始写入当前时间的数据
        # 写入交换机数据
        try:
            for switch in _switches:
                if not isinstance(switch.info_time, str):
                    cursor.execute(
                        "INSERT INTO '" + switch.ip + "' VALUES ('" + str(int(switch.info_time)) + "', '" + str(
                            switch.cpu_load) + "', '" + str(switch.mem_used) + "', '" + str(switch.temp) + "')")
        finally:
            conn.commit()
            cursor.close()
            conn.close()
            lock_data_history.release()
        # 写入端口数据
        try:
            for port in port_list:
                port_name = port[0:port.rfind(",")]
                switch_info = port.split(',')
                if len(switch_info) == 3:  # 排除空行或不正常的行
                    switch_ip = switch_info[0]
                    switch_port = switch_info[1]
                    for switch in _switches:
                        if switch.ip == switch_ip:
                            port_index = switch.if_name.index(switch_port)
                            if port_index != -1:
                                if len(switch.if_out_speed) != 0:
                                    cursor_flow.execute(
                                        "INSERT INTO '" + port_name + "' VALUES ('" + str(
                                            int(switch.info_time)) + "', '" + str(
                                            switch.if_in_speed[port_index]) + "', '" + str(
                                            switch.if_out_speed[port_index]) + "')")
                            else:
                                write_log("Port not found: " + port)
                            break
                    else:
                        pass
        finally:
            conn_flow.commit()
            cursor_flow.close()
            conn_flow.close()
            lock_flow_history.release()
        # write_log("写入数据库所用时间：" + str(time.time() - tmp_time))
        time.sleep(1)


def mission_distributer(ip_queue):  # 任务发放线程
    # 扫描线程数不建议超过交换机总数的1/3
    switches_num = len(switches)
    # 任务发放机制：队列小于1/3时增加1/3的交换机
    point = 0  # 指针，值为0、1、2，表示下次发放时从哪部分开始（0~1/3，1/3~2/3，2/3~1）
    one_time_num = switches_num // 3 + 1  # 一次发放的最多数量
    while 1:
        # print(ip_queue.qsize(), switches_num // 3)
        if ip_queue.qsize() <= switches_num // 3:
            for switch in switches[point * one_time_num:(point + 1) * one_time_num]:
                ip_queue.put(pickle.dumps(switch))
            point += 1
            if point == 3: point = 0
        else:
            time.sleep(10)  # 按一次轮询60秒计，三分之一需要20秒，这里取一半


def data_supervisor():  # 监控线程，微信发送交换机在线情况变化消息及每日统计，执行每日自动重启任务。
    time.sleep(180)  # 启动程序180s后再启动监控线程
    devices_alerted = []
    while (1):
        for switch in switches:
            try:
                if switch.down_time == "在线":
                    if switch.ip in devices_alerted:
                        devices_alerted.remove(switch.ip)
                        send_weixin_msg("[监控消息]交换机复活啦！\n" + switch.building_belong + switch.ip, 6)  # 发送消息
                        write_log(switch.ip + "上线")
                elif (time.time() - switch.down_time) / 60 >= SEND_MSG_DELAY and not (
                        switch.ip in devices_alerted):
                    send_weixin_msg("[监控消息]交换机炸了！\n" + switch.building_belong + switch.ip, 6)  # 发送消息
                    write_log(switch.ip + "掉线")
                    devices_alerted.append(switch.ip)
            except:
                write_log("Exception@data_supervisor: " + switch.ip + " switch.down_time " + switch.down_time)
        sleep60 = False
        if time.localtime()[3] == WEIXIN_STAT_TIME_H and time.localtime()[4] == WEIXIN_STAT_TIME_M:  # 每天发送统计信息
            send_weixin_stat()
            sleep60 = True
        if time.localtime()[3] == SW_REBOOT_TIME_H and time.localtime()[4] == SW_REBOOT_TIME_M:  # 每天重启过载交换机
            reboot_overload_sw()
            sleep60 = True
        # 内存Debug
        if time.localtime()[3] % 6 == 0 and time.localtime()[4] == 0:  # 每6小时写入内存使用率信息
            server_info = {}
            virtual_memory = psutil.virtual_memory()
            server_info["mem_total"] = round(virtual_memory[0] / 1024 / 1024, 2)
            server_info["mem_used"] = round(virtual_memory[3] / 1024 / 1024, 2)
            server_info["mem_used_2"] = round(server_info["mem_used"] * 100 / server_info["mem_total"], 2)
            send_weixin_msg("服务器内存使用率：" + str(server_info["mem_used_2"]) + "%", 2)
            sleep60 = True
        if sleep60 == True: time.sleep(60)
        time.sleep(1)


def send_weixin_stat():  # 统计掉线、CPU使用率过高、内存使用率过高、温度过高四种情况，暂时不统计端口流量过高
    msg_src = "[监控消息]今日交换机状态统计\n"
    msg = msg_src
    down = 0
    cpu_overload = 0
    men_overload = 0
    high_temp = 0
    for switch in switches:
        if switch.down_time != "在线":
            msg += switch.building_belong + switch.ip + "(" + switch.model + ") 掉线时间" + time.strftime(
                '%m-%d %H:%M] ', time.localtime(switch.down_time)) + "\n"
            down += 1
        try:  # 如果内容是“获取失败”或“设备不支持”就会发生异常，所以用try...except来忽略
            if switch.cpu_load >= CPU_THRESHOLD:
                print(switch.cpu_load)
                msg += switch.building_belong + switch.ip + "(" + switch.model + ") CPU使用率：" + str(
                    switch.cpu_load) + "%\n"
                cpu_overload += 1
        except:
            pass
        try:
            if switch.mem_used >= MEM_THRESHOLD:
                msg += switch.building_belong + switch.ip + "(" + switch.model + ") 内存使用率：" + str(
                    switch.mem_used) + "%\n"
                men_overload += 1
        except:
            pass
        try:
            if switch.temp >= TEMP_THRESHOLD:
                msg += switch.building_belong + switch.ip + "(" + switch.model + ") 温度过高：" + str(
                    switch.temp) + "℃\n"
                high_temp += 1
        except:
            pass
    if down > 0: msg += "共" + str(down) + "台交换机掉线\n"
    if cpu_overload > 0: msg += "共" + str(cpu_overload) + "台交换机CPU使用率过高\n"
    if men_overload > 0: msg += "共" + str(men_overload) + "台交换机内存使用率过高\n"
    if high_temp > 0: msg += "共" + str(high_temp) + "台交换机过热\n"
    if msg == msg_src: msg += "所有交换机正常！"
    send_weixin_msg(msg.rstrip(), 6)  # 用rstrip去掉最后的换行符


def reboot_overload_sw():  # 每天自动重启过载交换机
    ips = []
    for switch in switches:
        try:  # 如果内容是“获取失败”或“设备不支持”就会发生异常，所以用try...except来忽略
            if switch.cpu_load >= CPU_THRESHOLD: ips.append(switch.ip)
        except:
            pass
        try:
            if switch.mem_used >= MEM_THRESHOLD: ips.append(switch.ip)
        except:
            pass
        try:
            if switch.temp >= TEMP_THRESHOLD: ips.append(switch.ip)
        except:
            pass
    reboot_switches(ips)


def write_db(ip, column, data):
    global lock_data
    lock_data.acquire()
    conn = sqlite3.connect("data.db")
    cursor = conn.cursor()
    try:
        cursor.execute("UPDATE switches SET " + column + " = '" + data + "' WHERE ip = '" + ip + "'")
        conn.commit()
    finally:
        cursor.close()
        conn.close()
        lock_data.release()


def write_log(text):
    log = time.strftime('[%Y-%m-%d %H:%M:%S] ', time.localtime()) + text
    print(log)
    with open('log.txt', mode='a', encoding='utf-8') as f:
        f.write(log + "\n")


app = Flask(__name__)
app.secret_key = 'nia_sbA0Zr98j/3yX R~XHH!jmN]LWX/,?RT(*&^%$_W'


def startweb():
    if USE_HTTPS:
        app.run(host='0.0.0.0', port=WEB_PORT, ssl_context='adhoc')  # 如果需要用自己的证书，则修改ssl_context
    else:
        app.run(host='0.0.0.0', port=WEB_PORT)


def data_stream(data):  # Flask框架返回大量数据时，使用数据流的方式
    length = 10240  # 10kB
    t = len(data) // length + 1
    for a in range(0, t):
        b = data[a * length:(a + 1) * length]
        if not b: break
        yield b


# 主页
@app.route('/')
def index():
    global cpu_state
    global switch_down_stat
    if 'username' in session:
        return render_template('home_page.html', username=escape(session['username']))
    return redirect(url_for('login'))


# 登录页
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        form_username = request.form['username']
        form_password = request.form['password']
        login = False
        if form_username == WEB_USERNAME and form_password == WEB_PASSWORD:
            login = True
        if form_username == ADMIN_USERNAME and form_password == ADMIN_PASSWORD:
            login = True
        if login == True:
            session['username'] = request.form['username']
            return redirect(url_for('index'))
        else:
            return render_template('login.html', info="用户名或密码错！")
    return render_template('login.html')


# 注销
@app.route('/logout')
def logout():
    session.pop('username', None)
    return redirect(url_for('login'))


# 设备信息页
@app.route('/buildings')
def buildings():
    if 'username' in session:
        return render_template('buildings.html')
    else:
        return "未登录！"


# 设备具体信息页
@app.route('/devices')
def devices():
    if 'username' in session:
        return render_template('devices.html')
    else:
        return "未登录！"


# 端口监控信息页
@app.route('/ports')
def ports():
    if 'username' in session:
        return render_template('ports.html')
    else:
        return "未登录！"


# 端口流量信息页
@app.route('/port')
def port():
    if 'username' in session:
        return render_template('port.html')
    else:
        return "未登录！"


# 设置页
@app.route('/settings')
def settings():
    if 'username' in session:
        return render_template('settings.html')
        '''
        if session['username'] == ADMIN_USERNAME:
            return render_template('settings.html')
        else:
            return "权限不足！"
        '''
    else:
        return "未登录！"


# API，返回楼栋名称列表
@app.route('/api/buildings_list')
def api_buildings_list():
    global buildings_list
    return json.dumps(buildings_list, ensure_ascii=False)


# API，返回楼栋信息
@app.route('/api/building/<building_name>')
def api_building_name(building_name):
    info = []
    for switch in switches:
        if switch.building_belong == building_name:
            info.append({"ip": switch.ip, "model": switch.model, "desc": switch.desc, "down_time": switch.down_time,
                         "name": switch.name, "cpu_load": switch.cpu_load, "mem_used": switch.mem_used,
                         "temp": switch.temp, "up_time": switch.up_time, "info_time": switch.info_time})
    return json.dumps(info, ensure_ascii=False)


# API，返回报警信息
@app.route('/api/warnings')
def api_warnings():
    info = []
    for switch in switches:
        if switch.down_time != "在线":
            info.append(
                {"ip": switch.ip, "model": switch.model, "warning": "devices_down", "down_time": switch.down_time})
        try:  # 如果内容是“获取失败”或“设备不支持”就会发生异常，所以用try...except来忽略
            if int(switch.cpu_load) >= CPU_THRESHOLD:
                info.append({"ip": switch.ip, "model": switch.model, "warning": "cpu_overload",
                             "cpu_load": switch.cpu_load})
        except:
            pass
        try:
            if int(switch.mem_used) >= MEM_THRESHOLD:
                info.append({"ip": switch.ip, "model": switch.model, "warning": "mem_overload",
                             "mem_used": switch.mem_used})
        except:
            pass
        try:
            if int(switch.temp) >= TEMP_THRESHOLD:
                info.append({"ip": switch.ip, "model": switch.model, "warning": "heat", "temp": switch.temp})
        except:
            pass
        try:
            for a in range(0, len(switch.if_name)):
                if switch.if_in_speed[a] * 8 >= int(switch.if_speed[a]) * 1024 * 1024 * IF_SPEED_THRESHOLD and int(
                        switch.if_speed[a]) != 0:
                    info.append(
                        {"ip": switch.ip, "model": switch.model, "warning": "if_in", "if_name": switch.if_name[a],
                         "if_speed_info": str(round(switch.if_in_speed[a] / 1024 / 1024 * 8)) + "/" + switch.if_speed[
                             a]})
                if switch.if_out_speed[a] * 8 >= int(switch.if_speed[a]) * 1024 * 1024 * IF_SPEED_THRESHOLD and int(
                        switch.if_speed[a]) != 0:
                    info.append(
                        {"ip": switch.ip, "model": switch.model, "warning": "if_out", "if_name": switch.if_name[a],
                         "if_speed_info": str(round(switch.if_out_speed[a] / 1024 / 1024 * 8)) + "/" + switch.if_speed[
                             a]})
        except:
            pass
    return json.dumps(info, ensure_ascii=False)


# API，返回属性数据
@app.route('/api/<attr>')
def api_stat(attr):
    global port_list
    if attr == "ports":
        info = port_list
    else:
        info = []
        for switch in switches:
            if attr == "down_time": info.append(switch.down_time)
            if attr == "cpu_load": info.append(switch.cpu_load)
            if attr == "mem_used": info.append(switch.mem_used)
            if attr == "temp": info.append(switch.temp)
    return json.dumps(info, ensure_ascii=False)


# API，返回设备信息
@app.route('/api/devices/<ip>')
def api_devices(ip):
    info = {}
    for switch in switches:
        if switch.ip == ip:
            if_ip = []
            for index in switch.if_index:
                if index in switch.if_ipindex:
                    if_ip.append(switch.if_ip[switch.if_ipindex.index(index)] + " / " + switch.if_ipmask[
                        switch.if_ipindex.index(index)])
                else:
                    if_ip.append(' ')
            info = {"if_name": switch.if_name, "if_descr": switch.if_descr, "if_status": switch.if_status,
                    "if_uptime": switch.if_uptime, "if_ip": if_ip, "if_in": switch.if_in, "if_out": switch.if_out,
                    "if_in_speed": switch.if_in_speed, "if_out_speed": switch.if_out_speed, "if_speed": switch.if_speed}
    return json.dumps(info, ensure_ascii=False)


# API,返回历史数据信息
@app.route('/api/history/<ip>')
def api_history(ip):
    lock_data_history.acquire()
    conn = sqlite3.connect("data_history.db")
    cursor = conn.cursor()
    tmp_time = time.time()
    try:
        cursor.execute("SELECT * FROM '" + ip + "'")
        values = cursor.fetchall()
    finally:
        cursor.close()
        conn.close()
        lock_data_history.release()
    his_dict = {}
    for a in values:
        his_dict[a[0]] = {'cpu': a[1], 'mem': a[2], 'temp': a[3]}
    # print("查询历史数据消耗时间：", time.time() - tmp_time)
    a = json.dumps(his_dict, ensure_ascii=False)
    return Response(data_stream(a), mimetype='application/json')  # 数据量太大，使用数据流的方式返回


# API,返回流量速率历史数据信息
@app.route('/api/flow_history/<port>')
def api_flow_history(port):
    port = port.replace("_", "/")
    lock_flow_history.acquire()
    conn = sqlite3.connect("flow_history.db")
    cursor = conn.cursor()
    tmp_time = time.time()
    try:
        cursor.execute("SELECT * FROM '" + port + "'")
        values = cursor.fetchall()
    finally:
        cursor.close()
        conn.close()
        lock_flow_history.release()
    his_dict = {}
    for a in values:
        his_dict[a[0]] = {'in': a[1], 'out': a[2]}
    # print("查询流量历史数据消耗时间：", time.time() - tmp_time)
    a = json.dumps(his_dict, ensure_ascii=False)
    return Response(data_stream(a), mimetype='application/json')  # 数据量太大，使用数据流的方式返回


# API，设置微信统计发送时间
@app.route('/api/settings/weixin_stat_time', methods=['GET', 'POST'])
def weixin_stat_time():
    global WEIXIN_STAT_TIME_H
    global WEIXIN_STAT_TIME_M
    if 'username' in session:
        if request.method == 'POST':
            if session['username'] == ADMIN_USERNAME:
                # 写入到Config.py
                with open('Config.py', mode='r', encoding='utf-8') as f:
                    config = f.read()
                config = config.replace("WEIXIN_STAT_TIME_H = " + str(WEIXIN_STAT_TIME_H),
                                        "WEIXIN_STAT_TIME_H = " + request.form['time_h'])
                config = config.replace("WEIXIN_STAT_TIME_M = " + str(WEIXIN_STAT_TIME_M),
                                        "WEIXIN_STAT_TIME_M = " + request.form['time_m'])
                print(config)
                with open('Config.py', mode='w', encoding='utf-8') as f:
                    f.write(config)
                WEIXIN_STAT_TIME_H = int(request.form['time_h'])
                WEIXIN_STAT_TIME_M = int(request.form['time_m'])
            else:
                info = {"error_code": 1, "description": "权限不足！"}
                return json.dumps(info, ensure_ascii=False)
        # 返回当前数值
        info = {"error_code": 0, "time_h": WEIXIN_STAT_TIME_H, "time_m": WEIXIN_STAT_TIME_M}
        return json.dumps(info, ensure_ascii=False)
    else:
        return "未登录！"


# API，设置自动重启时间
@app.route('/api/settings/sw_reboot_time', methods=['GET', 'POST'])
def sw_reboot_time():
    global SW_REBOOT_TIME_H, SW_REBOOT_TIME_M
    if 'username' in session:
        if request.method == 'POST':
            if session['username'] == ADMIN_USERNAME:
                # 写入到Config.py
                with open('Config.py', mode='r', encoding='utf-8') as f:
                    config = f.read()
                config = config.replace("SW_REBOOT_TIME_H = " + str(SW_REBOOT_TIME_H),
                                        "SW_REBOOT_TIME_H = " + request.form['time_h'])
                config = config.replace("SW_REBOOT_TIME_M = " + str(SW_REBOOT_TIME_M),
                                        "SW_REBOOT_TIME_H = " + request.form['time_m'])
                with open('Config.py', mode='w', encoding='utf-8') as f:
                    f.write(config)
                SW_REBOOT_TIME_H = int(request.form['time_h'])
                SW_REBOOT_TIME_M = int(request.form['time_m'])
            else:
                info = {"error_code": 1, "description": "权限不足！"}
                return json.dumps(info, ensure_ascii=False)
        info = {"error_code": 0, "time_h": SW_REBOOT_TIME_H, "time_m": SW_REBOOT_TIME_M}
        return json.dumps(info, ensure_ascii=False)
    else:
        return "未登录！"


# API，重启交换机
@app.route('/api/tools/reboot_switches', methods=['POST'])
def reboot_sw():
    if 'username' in session:
        if session['username'] == ADMIN_USERNAME:
            ip = request.form['ip']
            reboot_switch_snmp(ip)
            return "监控消息：已发送重启命令！请稍后查看交换机状态。"  # TODO：显示重启进度
        else:
            return "权限不足！"
    else:
        return "未登录！"


# API，发送微信统计。
@app.route('/api/tools/send_wx_stat')  # 这个应该不用限制管理员权限吧……？
def send_wx_stat():
    if 'username' in session:
        if session['username'] == ADMIN_USERNAME:
            send_weixin_stat()
            return "发送成功！"
        else:
            return "权限不足！"
    else:
        return "未登录！"


# API，立即重启扫描进程
@app.route('/api/reboot_scan_process')
def api_reboot_scan_process():
    if 'username' in session:
        if session['username'] == ADMIN_USERNAME:
            global scan_processes, ip_queue, recive_queue
            write_log("用户手动重启扫描进程。现在内存使用率为" + str(psutil.virtual_memory()[2]) + "%")
            Global.reboot = True
            for a in range(0, SCAN_PROCESS):
                scan_processes[a].join()
            Global.reboot = False
            scan_processes = []
            for a in range(0, SCAN_PROCESS):
                scan_processes.append(
                    Process(target=scan_process, name="扫描进程" + str(a), args=(ip_queue, recive_queue,)))
                scan_processes[a].start()
            return "重启成功！"
        else:
            return "权限不足！"
    else:
        return "未登录！"


# API，返回服务器信息
@app.route('/api/server_info')
def api_server_info():
    if 'username' in session:
        server_info = {}
        virtual_memory = psutil.virtual_memory()
        server_info["mem_total"] = round(virtual_memory[0] / 1024 / 1024, 2)
        server_info["mem_free"] = round(virtual_memory[4] / 1024 / 1024, 2)
        server_info["mem_used"] = round(virtual_memory[3] / 1024 / 1024, 2)
        server_info["mem_buffers"] = round(virtual_memory[7] / 1024 / 1024, 2)
        server_info["mem_cached"] = round(virtual_memory[8] / 1024 / 1024, 2)
        server_info["mem_free_2"] = round(server_info["mem_free"] * 100 / server_info["mem_total"], 2)
        server_info["mem_used_2"] = round(server_info["mem_used"] * 100 / server_info["mem_total"], 2)
        server_info["mem_buffers_2"] = round(server_info["mem_buffers"] * 100 / server_info["mem_total"], 2)
        server_info["mem_cached_2"] = round(server_info["mem_cached"] * 100 / server_info["mem_total"], 2)
        swap_memory = psutil.swap_memory()
        server_info["swap_total"] = round(swap_memory[0] / 1024 / 1024, 2)
        server_info["swap_used"] = round(swap_memory[1] / 1024 / 1024, 2)
        server_info["swap_used_2"] = swap_memory[3]
        # cpu_percent=psutil.cpu_percent(interval=None, percpu=False)
        return json.dumps(server_info, ensure_ascii=False)
    else:
        return "未登录！"


# API，返回日志
@app.route('/api/log')
def api_log():
    if 'username' in session:
        if session['username'] == ADMIN_USERNAME:
            with open("log.txt", "r", encoding='utf-8') as f:
                log = f.read()
            return Response(data_stream(log), mimetype='text/plain')
        else:
            return "权限不足！"
    else:
        return "未登录！"


# API，清除日志
@app.route('/api/clean_log')
def api_clean_log():
    if 'username' in session:
        if session['username'] == ADMIN_USERNAME:
            os.remove("log.txt")
            os.mknod("log.txt")
            return "清除成功！"
        else:
            return "权限不足！"
    else:
        return "未登录！"


# API，发送微信全体消息
@app.route('/api/send_weixin_msg', methods=['POST'])
def api_send_weixin_msg():
    if 'username' in session:
        if session['username'] == ADMIN_USERNAME:
            send_weixin_msg(request.form['msg'], 6)
            return "发送成功！"
        else:
            return "权限不足！"
    else:
        return "未登录！"


# （测试用）API，返回CPU状态未知的交换机
@app.route('/api/cpu_unknown')
def api_snmp_warning():
    info = []
    for switch in switches:
        if switch.cpu_load == "等待获取" or switch.cpu_load == "获取失败":
            info.append(switch.ip)
    return json.dumps(info, ensure_ascii=False)


# （测试用）返回登录用户名
@app.route('/api/username')
def api_test():
    if 'username' in session:
        print(session)
        return session['username']
    else:
        return "未登录！"


def start_web():
    # 启动web界面。注：生产环境部署参考http://docs.jinkan.org/docs/flask/deploying/index.html
    threading.Thread(target=startweb, name="线程_flask").start()


# start_switch_monitor()

if __name__ == '__main__':
    start_switch_monitor()
    start_web()
