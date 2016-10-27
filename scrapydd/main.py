# -*-coding:utf8-*-
import tornado.ioloop
import tornado.web
import tornado.template
from scrapyd.eggstorage import FilesystemEggStorage
import scrapyd.config
from cStringIO import StringIO
from models import Session, Project, Spider, Trigger, SpiderExecutionQueue, Node, init_database, HistoricalJob, \
    SpiderWebhook, session_scope
from schedule import SchedulerManager
from .nodes import NodeManager
import datetime
import json
from .config import Config
import os.path
import sys
import logging
from .exceptions import *
from sqlalchemy import desc
from optparse import OptionParser, OptionValueError
import subprocess
import signal
from stream import PostDataStreamer
from webhook import WebhookDaemon
import tempfile
import pkg_resources
import shutil
from daemonize import daemonize
from tornado.process import Subprocess
from tornado import gen
import tornado.httpserver
import tornado.netutil

logger = logging.getLogger(__name__)

def get_template_loader():
    loader = tornado.template.Loader(os.path.join(os.path.dirname(__file__), "templates"))
    return loader

def get_egg_storage():
    return FilesystemEggStorage(scrapyd.config.Config())

class MainHandler(tornado.web.RequestHandler):
    def get(self):
        session = Session()
        projects = list(session.query(Project))
        loader = get_template_loader()
        self.write(loader.load("index.html").generate(projects=projects))
        session.close()

class UploadProject(tornado.web.RequestHandler):
    @gen.coroutine
    def post(self):
        egg_storage = get_egg_storage()
        project_name = self.request.arguments['project'][0]
        version = self.request.arguments['version'][0]
        eggfile = self.request.files['egg'][0]
        eggfilename = eggfile['filename']
        eggf = StringIO(eggfile['body'])
        egg_storage.put(eggf, project_name, version)

        project_workspace_dir = os.path.abspath(os.path.join('workspace', project_name))
        if sys.platform.startswith('linux'):
            pip = os.path.join(project_workspace_dir, 'bin', 'pip')
            python = os.path.join(project_workspace_dir, 'bin', 'python')
        elif sys.platform.startswith('win'):
            pip = os.path.join(project_workspace_dir, 'Scripts', 'pip.exe')
            python = os.path.join(project_workspace_dir, 'Scripts', 'python.exe')
            # since the tornado.process.Subprocess.wait_for_exit does not support
            # windows platform now, the windows platform is not supported yet.
            raise NotImplementedError('Unsupported system %s' % sys.platform)
        else:
            raise NotImplementedError('Unsupported system %s' % sys.platform)
        if not os.path.exists(pip) or not os.path.exists(python):
            logger.debug('Virtualenv environment does not exist, creating.')
            yield Subprocess(['virtualenv', '--system-site-packages', project_workspace_dir]).wait_for_exit()
        try:
            prefix = '%s-%s-' % (project_name, version)
            fd, eggpath = tempfile.mkstemp(prefix=prefix, suffix='.egg')
            logger.debug('tmp egg file saved to %s' % eggpath)
            lf = os.fdopen(fd, 'wb')
            eggf.seek(0)
            shutil.copyfileobj(eggf, lf)
            lf.close()
            try:
                d = pkg_resources.find_distributions(eggpath).next()
            except StopIteration:
                raise ValueError("Unknown or corrupt egg")
            requirements = [str(x) for x in d.requires()]
        finally:
            if eggpath:
                os.remove(eggpath)
        # install requirements in virtualenv
        yield Subprocess([pip, 'install'] + requirements).wait_for_exit()

        env = os.environ.copy()
        env['SCRAPY_PROJECT'] = project_name
        spider_list_process = Subprocess([python, '-m', 'scrapyd.runner', 'list'], env=env, stdout=subprocess.PIPE)
        yield spider_list_process.wait_for_exit()
        spiders = spider_list_process.stdout.read().splitlines()
        logger.debug('spiders: %s' % spiders)

        with session_scope() as session:
            project = session.query(Project).filter_by(name=project_name).first()
            if project is None:
                project = Project()
                project.name = project_name
                project.version = version
                session.add(project)
                session.commit()
                session.refresh(project)

            for spider_name in spiders:
                spider = session.query(Spider).filter_by(project_id = project.id, name=spider_name).first()
                if spider is None:
                    spider = Spider()
                    spider.name = spider_name
                    spider.project_id = project.id
                    session.add(spider)
                    session.commit()
            if self.request.path.endswith('.json'):
                self.write(json.dumps({'status': 'ok', 'spiders': len(spiders)}))
            else:
                loader = get_template_loader()
                self.write(loader.load("uploadproject.html").generate(myvalue="XXX"))

    def get(self):
        loader = get_template_loader()
        self.write(loader.load("uploadproject.html").generate(myvalue="XXX"))

class ScheduleHandler(tornado.web.RequestHandler):
    def initialize(self, scheduler_manager):
        self.scheduler_manager = scheduler_manager

    def post(self):
        project = self.get_argument('project')
        spider = self.get_argument('spider')

        try:
            job = self.scheduler_manager.add_task(project, spider)
            jobid = job.id
            response_data = {
                'status':'ok',
                'jobid': jobid
            }
            self.write(json.dumps(response_data))
        except JobRunning as e:
            response_data = {
                'status':'error',
                'errormsg': 'job is running with jobid %s' % e.jobid
            }
            self.set_status(400, 'job is running')
            self.write(json.dumps(response_data))


class AddScheduleHandler(tornado.web.RequestHandler):
    def initialize(self, scheduler_manager):
        self.scheduler_manager = scheduler_manager

    def post(self):
        project = self.get_argument('project')
        spider = self.get_argument('spider')
        cron = self.get_argument('cron')
        try:
            self.scheduler_manager.add_schedule(project, spider, cron)
            response_data = {
                'status':'ok',
            }
            self.write(json.dumps(response_data))
        except SpiderNotFound:
            response_data = {
                'status':'error',
                'errormsg': 'spider not found',
            }
            self.write(json.dumps(response_data))
        except ProjectNotFound:
            response_data = {
                'status':'error',
                'errormsg': 'project not found',
            }
            self.write(json.dumps(response_data))
        except InvalidCronExpression:
            response_data = {
                'status':'error',
                'errormsg': 'invalid cron expression.',
            }
            self.write(json.dumps(response_data))


class ProjectList(tornado.web.RequestHandler):
    def get(self):
        session = Session()
        projects = session.query(Project)

        response_data = {'projects':{'id': item.id for item in projects}}
        self.write(response_data)
        session.close()

class SpiderInstanceHandler(tornado.web.RequestHandler):
    def get(self, id):
        session = Session()
        spider = session.query(Spider).filter_by(id=id).first()
        loader = get_template_loader()
        self.write(loader.load("spider.html").generate(spider=spider))
        session.close()

class SpiderInstanceHandler2(tornado.web.RequestHandler):
    def get(self, project, spider):
        session = Session()
        project = session.query(Project).filter(Project.name == project).first()
        spider = session.query(Spider).filter(Spider.project_id == project.id, Spider.name == spider).first()
        jobs = session.query(HistoricalJob)\
            .filter(HistoricalJob.spider_id == spider.id)\
            .order_by(desc(HistoricalJob.start_time))\
            .slice(0, 100)
        webhook = session.query(SpiderWebhook).filter_by(id=spider.id).first()
        context = {}
        context['spider'] = spider
        context['jobs'] = jobs
        context['webhook'] = webhook
        loader = get_template_loader()
        self.write(loader.load("spider.html").generate(**context))
        session.close()

class SpiderEggHandler(tornado.web.RequestHandler):
    def get(self, id):
        session = Session()
        spider = session.query(Spider).filter_by(id=id).first()
        egg_storage = get_egg_storage()
        version, f = egg_storage.get(spider.project.name)
        self.write(f.read())
        session.close()


class SpiderListHandler(tornado.web.RequestHandler):
    def get(self):
        session = Session()
        spiders = session.query(Spider)
        loader = get_template_loader()
        self.write(loader.load("spiderlist.html").generate(spiders=spiders))
        session.close()


class SpiderTriggersHandler(tornado.web.RequestHandler):
    def initialize(self, scheduler_manager):
        self.scheduler_manager = scheduler_manager

    def get(self, project, spider):
        session = Session()
        project = session.query(Project).filter(Project.name == project).first()
        spider = session.query(Spider).filter(Spider.project_id == project.id, Spider.name == spider).first()
        loader = get_template_loader()
        self.write(loader.load("spidercreatetrigger.html").generate(spider=spider))

        session.close()

    def post(self, project, spider):
        cron = self.get_argument('cron')
        self.scheduler_manager.add_schedule(project, spider, cron)
        self.redirect('/projects/%s/spiders/%s'% (project, spider))

class  ExecuteNextHandler(tornado.web.RequestHandler):
    def initialize(self, scheduler_manager):
        self.scheduler_manager = scheduler_manager

    def post(self):
        session = Session()
        node_id = int(self.request.arguments['node_id'][0])
        next_task = self.scheduler_manager.get_next_task(node_id)

        response_data = {'data': None}

        if next_task is not None:
            spider = session.query(Spider).filter_by(id=next_task.spider_id).first()
            project = session.query(Project).filter_by(id=spider.project_id).first()
            response_data['data'] = {'task':{
                'task_id': next_task.id,
                'spider_id':  next_task.spider_id,
                'spider_name': next_task.spider_name,
                'project_name': next_task.project_name,
                'version': project.version,
            }}
        self.write(json.dumps(response_data))
        session.close()


@tornado.web.stream_request_body
class ExecuteCompleteHandler(tornado.web.RequestHandler):
    def initialize(self, webhook_daemon, scheduler_manager):
        '''

        @type webhook_daemon : WebhookDaemon
        :return:
        '''
        self.webhook_daemon = webhook_daemon
        self.scheduler_manager = scheduler_manager

    #
    def prepare(self):
        MB = 1024 * 1024
        GB = 1024 * MB
        TB = 1024 * GB
        MAX_STREAMED_SIZE = 1 * GB
        # set the max size limiation here
        self.request.connection.set_max_body_size(MAX_STREAMED_SIZE)
        try:
            total = int(self.request.headers.get("Content-Length", "0"))
        except:
            total = 0
        self.ps = PostDataStreamer(total)  # ,tmpdir="/tmp"

        # self.fout = open("raw_received.dat","wb+")

    def post(self):
        try:
            self.ps.finish_receive()
            fields = self.ps.get_values(['task_id', 'log', 'status'])
            logger.debug(self.ps.get_nonfile_names())
            node_id = self.request.headers.get('X-Dd-Nodeid')
            task_id = fields['task_id']
            log = fields.get('log', None)
            status = fields['status']
            if status == 'success':
                status_int = 2
            elif status == 'fail':
                status_int = 3
            else:
                self.write_error(401, 'Invalid argument: status.')
                return

            session = Session()
            query = session.query(SpiderExecutionQueue).filter(SpiderExecutionQueue.id == task_id, SpiderExecutionQueue.status == 1)
            # be compatible with old agent version
            if node_id:
                query = query.filter(SpiderExecutionQueue.node_id == node_id)
            else:
                logger.warning('Agent has not specified node id in complete request, client address: %s.' % self.request.remote_ip)
            job = query.first()

            if job is None:
                self.write_error(404, 'Job not found.')
                session.close()
                return
            log_file = None
            items_file = None
            try:
                spider_log_folder = os.path.join('logs', job.project_name, job.spider_name)
                if not os.path.exists(spider_log_folder):
                    os.makedirs(spider_log_folder)
                log_file = os.path.join(spider_log_folder, job.id + '.log')
                log_part = self.ps.get_parts_by_name('log')[0]
                if log_part:
                    import shutil
                    shutil.copy(log_part['tmpfile'].name, log_file)
                elif log:
                    with open(log_file, 'w') as f:
                        f.write(log)

            except Exception as e:
                logger.error('Error when writing task log file, %s' % e)

            try:
                part = self.ps.get_parts_by_name('items')[0]
                tmpfile = part['tmpfile'].name
                logger.debug('tmpfile size: %d' % os.path.getsize(tmpfile))
                items_file_path = os.path.join('items', job.project_name, job.spider_name)
                if not os.path.exists(items_file_path):
                    os.makedirs(items_file_path)
                items_file = os.path.join(items_file_path, '%s.jl' % job.id)
                import shutil
                shutil.copy(tmpfile, items_file)
                logger.debug('item file size: %d' % os.path.getsize(items_file))
            except Exception as e:
                logger.error('Error when writing items file, %s' % e)

            job.status = status_int
            job.update_time = datetime.datetime.now()
            historical_job = self.scheduler_manager.job_finished(job, log_file, items_file)
            if items_file:
                self.webhook_daemon.on_spider_complete(historical_job, items_file)

            session.close()
            logger.info('Job %s completed.' % task_id)
            response_data = {'status': 'ok'}
            self.write(json.dumps(response_data))


        finally:
            # Don't forget to release temporary files.
            self.ps.release_parts()


    def data_received(self, chunk):
        # self.fout.write(chunk)
        self.ps.receive(chunk)


class NodesHandler(tornado.web.RequestHandler):
    def initialize(self, node_manager):
        self.node_manager = node_manager

    def post(self):
        node = self.node_manager.create_node(self.request.remote_ip)
        self.write(json.dumps({'id': node.id}))


class NodeHeartbeatHandler(tornado.web.RequestHandler):
    def initialize(self, node_manager, scheduler_manager):
        self.node_manager = node_manager
        self.scheduler_manager = scheduler_manager

    def post(self, id):
        #logger.debug(self.request.headers)
        node_id = int(id)
        self.set_header('X-DD-New-Task', self.scheduler_manager.has_task())
        try:
            self.node_manager.heartbeat(node_id)
            running_jobs = self.request.headers.get('X-DD-RunningJobs', None)
            if running_jobs:
                self.scheduler_manager.jobs_running(node_id, running_jobs.split(','))
            response_data = {'status':'ok'}
        except NodeExpired:
            response_data = {'status': 'error', 'errmsg': 'Node expired'}
            self.set_status(400, 'Node expired')
        self.write(json.dumps(response_data))

class JobsHandler(tornado.web.RequestHandler):
    def initialize(self, scheduler_manager):
        self.scheduler_manager = scheduler_manager

    def get(self):
        pending, running, finished = self.scheduler_manager.jobs()
        context = {
            'pending': pending,
            'running': running,
            'finished': finished,
        }
        loader = get_template_loader()
        self.write(loader.load("jobs.html").generate(**context))

class LogsHandler(tornado.web.RequestHandler):
    def get(self, project, spider, jobid):
        logfilepath = os.path.join('logs', project, spider, jobid + '.log')
        with open(logfilepath, 'r') as f:
        #    self.write(f.read())
            log = f.read()
        loader = get_template_loader()
        self.write(loader.load("log.html").generate(log=log))

class JobStartHandler(tornado.web.RequestHandler):
    def initialize(self, scheduler_manager):
        self.scheduler_manager = scheduler_manager

    def post(self, jobid):
        pid = self.get_argument('pid')
        self.scheduler_manager.job_start(jobid, pid)


class SpiderWebhookHandler(tornado.web.RequestHandler):
    def get(self, project_name, spider_name):
        session = Session()
        project = session.query(Project).filter(Project.name == project_name).first()
        spider = session.query(Spider).filter(Spider.project_id == project.id, Spider.name == spider_name).first()

        webhook = session.query(SpiderWebhook).filter_by(id=spider.id).first()
        self.write(webhook.payload_url)

    def post(self, project_name, spider_name):
        payload_url = self.get_argument('payload_url')
        session = Session()
        project = session.query(Project).filter(Project.name == project_name).first()
        spider = session.query(Spider).filter(Spider.project_id == project.id, Spider.name == spider_name).first()

        webhook = session.query(SpiderWebhook).filter_by(id=spider.id).first()
        if webhook is None:
            webhook = SpiderWebhook()
            webhook.payload_url = payload_url
            webhook.id = spider.id
        session.add(webhook)
        session.commit()
        session.close()

    def put(self, project_name, spider_name):
        self.post(project_name, spider_name)

    def delete(self, project_name, spider_name):
        session = Session()
        project = session.query(Project).filter(Project.name == project_name).first()
        spider = session.query(Spider).filter(Spider.project_id == project.id, Spider.name == spider_name).first()
        session.query(SpiderWebhook).filter_by(id=spider.id).delete()
        session.commit()
        session.close()

def make_app(scheduler_manager, node_manager, webhook_daemon):
    '''

    @type scheduler_manager SchedulerManager
    @type node_manager NodeManager
    @type webhook_daemon: WebhookDaemon

    :return:
    '''
    return tornado.web.Application([
        (r"/", MainHandler),
        (r'/uploadproject', UploadProject),
        (r'/addversion.json', UploadProject),
        (r'/schedule.json', ScheduleHandler, {'scheduler_manager': scheduler_manager}),
        (r'/add_schedule.json', AddScheduleHandler, {'scheduler_manager': scheduler_manager}),
        (r'/projects', ProjectList),
        (r'/spiders', SpiderListHandler),
        (r'/spiders/(\d+)', SpiderInstanceHandler),
        (r'/spiders/(\d+)/egg', SpiderEggHandler),
        (r'/projects/(\w+)/spiders/(\w+)', SpiderInstanceHandler2),
        (r'/projects/(\w+)/spiders/(\w+)/triggers', SpiderTriggersHandler, {'scheduler_manager': scheduler_manager}),
        (r'/projects/(\w+)/spiders/(\w+)/webhook', SpiderWebhookHandler),
        (r'/executing/next_task', ExecuteNextHandler, {'scheduler_manager': scheduler_manager}),
        (r'/executing/complete', ExecuteCompleteHandler, {'webhook_daemon': webhook_daemon, 'scheduler_manager': scheduler_manager}),
        (r'/nodes', NodesHandler, {'node_manager': node_manager}),
        (r'/nodes/(\d+)/heartbeat', NodeHeartbeatHandler, {'node_manager': node_manager, 'scheduler_manager': scheduler_manager}),
        (r'/jobs', JobsHandler, {'scheduler_manager': scheduler_manager}),
        (r'/jobs/(\w+)/start', JobStartHandler, {'scheduler_manager': scheduler_manager}),
        (r'/logs/(\w+)/(\w+)/(\w+).log', LogsHandler),
    ])

def start_server(argv=None):
    config = Config()
    if config.getboolean('debug'):
        logging.basicConfig(level=logging.DEBUG, filename='scrapydd-server.log',
                                     format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    else:
        logging.basicConfig(level=logging.INFO, filename='scrapydd-server.log',
                                 format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')

    logging.debug('starting server with argv : %s' % str(argv))

    init_database()
    bind_address = config.get('bind_address')
    bind_port = config.getint('bind_port')
    print 'Starting server on %s:%d' % (bind_address, bind_port)

    sockets = tornado.netutil.bind_sockets(bind_port, bind_address)
    #tornado.process.fork_processes(4)

    scheduler_manager = SchedulerManager()
    scheduler_manager.init()

    node_manager = NodeManager(scheduler_manager)
    node_manager.init()

    webhook_daemon = WebhookDaemon()
    webhook_daemon.init()

    app = make_app(scheduler_manager, node_manager, webhook_daemon)

    server = tornado.httpserver.HTTPServer(app)
    server.add_sockets(sockets)
    ioloop = tornado.ioloop.IOLoop.current()
    ioloop.start()

def run(argv=None):
    if argv is None:
        argv = sys.argv
    parser = OptionParser(prog  = 'scrapydd server')
    parser.add_option('--daemon', action='store_true', help='run scrapydd server in daemon mode')
    parser.add_option('--pidfile', help='pid file will be created when daemon started')
    opts, args = parser.parse_args(argv)
    pidfile = opts.pidfile or 'scrapydd-server.pid'

    if opts.daemon:
        print 'starting daemon.'
        daemon = Daemon(pidfile=pidfile)
        daemon.start()
        sys.exit(0)
    else:
        start_server()

class Daemon():
    def __init__(self, pidfile):
        self.pidfile = pidfile
        self.subprocess_p = None
        self.pid = 0

    def start_subprocess(self):
        argv = sys.argv
        argv.remove('--daemon')
        pargs = argv
        env = os.environ.copy()
        self.subprocess_p = subprocess.Popen(pargs, env=env)
        signal.signal(signal.SIGINT, self.on_signal)
        signal.signal(signal.SIGTERM, self.on_signal)
        self.subprocess_p.wait()

    def read_pidfile(self):
        try:
            with open(self.pidfile, 'r') as f:
                return int(f.readline())
        except IOError:
            return None

    def try_remove_pidfile(self):
        if os.path.exists(self.pidfile):
            os.remove(self.pidfile)

    def on_signal(self, signum, frame):
        logger.info('receive signal %d closing' % signum)
        if self.subprocess_p:
            self.subprocess_p.terminate()
        self.try_remove_pidfile()
        tornado.ioloop.IOLoop.instance().stop()

    def start(self):
        signal.signal(signal.SIGINT, self.on_signal)
        signal.signal(signal.SIGTERM, self.on_signal)
        daemonize(pidfile=self.pidfile)
        self.start_subprocess()
        #start_server()
        self.try_remove_pidfile()

if __name__ == "__main__":
    run()
