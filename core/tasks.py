from celery.decorators import task
from celery.utils.log import get_task_logger
from celery import shared_task, task, chain
from subprocess import call, run, PIPE, Popen
from api.settings import BASE_DIR
from rest_framework.renderers import JSONRenderer
import os
import json
from .serializers import SetupSerializer, AnsiblePlaybookSerializer
from .models import Setup, AnsiblePlaybook, Key
from tempfile import NamedTemporaryFile
from jinja2 import Template
from os.path import isfile
import hashlib
import base64
import mongoengine
import stat

logger = get_task_logger(__name__)
env = os.environ

@shared_task
def generate_ssh_key(setup_id, user, cloud):
    setup = Setup.objects.get(id=setup_id)

    key_instance = None
    priv_key_path = ""
    try:
        key_instance = Key.objects.get(user=user, cloud=cloud)
        filename = base64.b64encode(hashlib.new('md5').digest()).decode('utf-8')
        priv_key_path = "/tmp/{0}".format(filename)

        key_private = open(priv_key_path, 'w+')
        key_public = open(priv_key_path + '.pub', 'w+')

        key_private.write(key_instance.private)
        key_private.close()

        key_public.write(key_instance.public)
        key_public.close()

        os.chmod(priv_key_path, stat.S_IRUSR)

    except mongoengine.errors.DoesNotExist:
        filename = base64.b64encode(hashlib.new('md5').digest()).decode('utf-8')
        comment = "Autogenerated OpsClick API"
        priv_key_path = "/root/.ssh/{0}".format(filename)
        command = ["/usr/bin/ssh-keygen", "-t", "rsa", "-N", "", "-f", priv_key_path, "-C", comment]

        try:
            if not isfile(priv_key_path):
                run(command)
        except:
            logger.warn("something wrong with ssh-keygen")

        key_private_file = open(priv_key_path, 'r')
        key_private_data = key_private_file.read()
        key_private_file.close()

        key_public_file = open(priv_key_path + '.pub', 'r')
        key_public_data = key_public_file.readline().strip('\n')
        key_public_file.close()

        key = Key(user=user,
                  cloud=cloud,
                  private=key_private_data,
                  public=key_public_data)
        key_instance = key.save()

    if key_instance:
        setup.update(key_id=key_instance)
    return priv_key_path


def running_setup(data):
    info = {
        'user': data['user'],
        'service': data['service'],
        'cloud': data['cloud'],
        'options': data['options'],
        'status': "Running"
    }
    setup = Setup(**info)
    setup_id = str((setup.save()).id)

    res = chain(generate_ssh_key.s(setup_id, data['user'], data['cloud']),
                ansible_setup.s(setup_id, data),
                install_docker.s(setup_id).set(countdown=5),
                install_service.s(setup_id, data['service'],
                                  conf_vars=data['options']['service_opts']))()
    return setup_id

@shared_task
def ansible_setup(info, setup_id, data=None):
    ssh_key_path = info
    setup = Setup.objects.get(id=setup_id)

    if not data:
        return
    (user, service, cloud, options) = data['user'], data['service'], data['cloud'], data['options']

    setup.update(status="Installing Server")

    key_file = open(ssh_key_path + ".pub", "r")
    ssh_key = key_file.readline().strip('\n')
    key_file.close()
    data['options']['ssh_pub_keys'].append({ 'name': "OpsClick API Deploy key to %s" % user,
                                             'key': ssh_key })

    cloud_path = "{0}/clouds/{1}".format(BASE_DIR, cloud)
    cloud_playbook = cloud_path + "/main.yml"
    cloud_host = cloud_path + "/hosts"
    logger.debug("%s" % options)

    if service and cloud:
        logger.debug("configuring the cloud %s" % (cloud))
        options.pop('service_opts')
        command = ['ansible-playbook', cloud_playbook, '-i', cloud_host, '--extra-vars', str(options)] 
        logger.debug(" ".join(command))

        env['ANSIBLE_CONFIG'] = cloud_path + "/ansible.cfg"
        #logger.info(command)
        ansible_call = Popen(command, stdout=PIPE, env=env)
        try:
            output, errs = ansible_call.communicate()
        except:
            logger.warn("something wrong with the ansible call")
            return

        try:
            outs  = json.loads(output.decode("utf-8"))
        except:
            logger.warn("something wrong with json decode")
            return

        pb_serializer = AnsiblePlaybookSerializer(data=outs)

        if pb_serializer.is_valid():
            pb_instance = pb_serializer.save()
            if pb_instance:
                setup.update(playbook=pb_instance)

        code = """
        function() {
            var droplets_ip = []
            db[collection].find(query).forEach(function(doc) {
                doc.plays.forEach(function(play) {
                    play.tasks.forEach(function(task) {
                        if(task.hosts.localhost.results) {
                            task.hosts.localhost.results.forEach(function(result) {
                                if(result.droplet) {
                                    droplets_ip.push(result.droplet.ip_address);
                                }
                            });
                        }
                    });
                });
            });
            if(droplets_ip.length > 0){
                return droplets_ip;
            }
            return false;
        }
        """
        ip_address = AnsiblePlaybook.objects.filter(id=pb_instance.id,).exec_js(code)

        return (ssh_key_path, ip_address)
    else:
        logger.warn("We need to know the service and the cloud")

@shared_task
def install_service(info, setup_id,  service, conf_vars={}):
    setup = Setup.objects.get(id=setup_id)
    setup.update(status="Installing application")

    try:
        ssh_key_path, hosts = info
    except TypeError as err:
        logger.error("{0}".format(err))
        return

    service_path = "{0}/services/{1}".format(BASE_DIR, service)
    service_playbook = service_path + "/main.yml"

    env['ANSIBLE_CONFIG'] = service_path + "/ansible.cfg"
    command = ['ansible-playbook', service_playbook, '-i', hosts, "--private-key", ssh_key_path, "--extra-vars", str(conf_vars)]
    ansible_call = Popen(command, stdout=PIPE, env=env)

    #logger.info(command)
    try:
        output, errs = ansible_call.communicate()
    except:
        print("Unexpected error:", sys.exc_info()[0])
        logger.warn("something wrong with ansible install service call")
        raise

    if ansible_call.returncode == 0:
        setup.update(status="DONE")
        return True

    setup.update(status="Error in setup")
    return False

@shared_task
def install_docker(info, setup_id):
    setup = Setup.objects.get(id=setup_id)
    setup.update(status="Preparing server for the application")

    try:
        ssh_key_path, hosts = info
    except TypeError as err:
        logger.error("{0}".format(err))
        return

    if not hosts:
        logger.error("You need to define the hosts")
        return

    docker_path = "{0}/clouds/lib/{1}".format(BASE_DIR, "docker")
    docker_playbook = docker_path + "/main.yml"
    inventory = """
    [targets]
    {% for host in hosts %}
    {{ host }}
    {% endfor %}
    """
    inventory_template = Template(inventory)
    rendered_inventory = inventory_template.render({'hosts': hosts})

    hosts = NamedTemporaryFile(delete=False)
    hosts.write(rendered_inventory.encode("utf-8"))
    hosts.close()

    env['ANSIBLE_CONFIG'] = docker_path + "/ansible.cfg"
    command = ['ansible-playbook', docker_playbook, '-i', hosts.name, "--private-key", ssh_key_path]
    ansible_call = Popen(command, stdout=PIPE, env=env)

    #logger.info(command)
    try:
        output, errs = ansible_call.communicate()
    except:
        print("Unexpected error:", sys.exc_info()[0])
        logger.warn("something wrong with ansible install docker call")
        raise

    if ansible_call.returncode == 0:
        logger.info("docker was installed")

    return ssh_key_path, hosts.name

