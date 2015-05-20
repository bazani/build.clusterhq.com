# Copyright Hybrid Logic Ltd.
"""
Configuration for a buildslave to run on PistonCloud

.. warning::
    This points at the staging buildserver by default.
"""
from pipes import quote as shellQuote
from fabric.api import sudo, task, env, execute, put, run, local
from fabric.context_managers import shell_env
from twisted.python.filepath import FilePath
from StringIO import StringIO
import yaml

# Since version 1.4.0, Fabric uses your ssh config (partly). However,
# you need to explicitly enable it.
# See http://stackoverflow.com/a/9685171
env.use_ssh_config = True

BUILDSLAVE_NAME = "flocker/functional/pistoncloud/centos-7/storage-driver"
BUILDSLAVE_NODENAME = "clusterhq_flocker_buildslave"
BUILDSLAVE_HOME = '/srv/buildslave'

# Be careful here! If our script has bugs we don't want to accidentally
# modify VMs or resources of another more important tenant
TENANT_NAME = "tmz-mdl-1"


def cmd(*args):
    return ' '.join(map(shellQuote, args))


def get_lastpass_config(key):
    """
    Download a section from LastPass.

    Requires you to have run ``lpass login`` first.
    """
    output = local(cmd('lpass', 'show', '--notes', key),
                   capture=True)
    config = yaml.safe_load(output.stdout)
    return config


def _configure_acceptance():
    """
    Download the entire acceptance.yml file from lastpass but only
    upload the metadata and pistoncloud credentials.
    """
    acceptance_config = {}
    full_config = get_lastpass_config(
        "acceptance@build.clusterhq.com"
    )
    for key in ('metadata', 'pistoncloud'):
        acceptance_config[key] = full_config['config'][key]

    put(
        StringIO(yaml.safe_dump(acceptance_config)),
        BUILDSLAVE_HOME + '/acceptance.yml',
        use_sudo=True,
    )


@task
def configure_acceptance():
    """
    Upload the pistoncloud acceptance credentials to the buildslave.
    """
    # The alias for the build slave server in ``.ssh/config``.
    env.hosts = ['pistoncloud-buildslave']
    execute(_configure_acceptance)


def put_template(template, replacements, remote_path, **put_kwargs):
    """
    Replace Python style string formatting variables in ``template``
    with the supplied ``replacements`` and then ``put`` the resulting
    content to ``remote_path``.
    """
    local_file = template.temporarySibling()
    try:
        with local_file.open('w') as f:
            content = template.getContent().format(
                **replacements
            )
            f.write(content)

        put(local_file.path, remote_path, **put_kwargs)
    finally:
        local_file.remove()


def set_google_dns():
    """
    Replace the ``/etc/resolv.conf`` file on the target server.

    XXX: This isn't a solution, but it at least allows the packages to
    install
    There is a documented permanent solution:
    * http://askubuntu.com/a/615951
    ...but it doesn't work.
    """
    put(
        StringIO(
            "\n".join([
                "nameserver 8.8.8.8"
                "nameserver 8.8.4.4"
            ]) + "\n"
        ),
        '/etc/resolv.conf',
        use_sudo=True,
        mode=0o644,
    )


def _create_server(
        keypair_name,
        # m1.large
        flavor=u'4',
        # SC_Centos7
        image=u'ab32525b-f565-49ca-9595-48cdb5eaa794',
        # tmz-mdl-net1
        net_id=u'74632532-1629-44b4-a464-dd31657f46a3',
):
    """
    Run ``nova boot`` to create a new server on which to run the
    PistonCloud build slave.
    """
    with shell_env(OS_TENANT_NAME=TENANT_NAME):
        run(
            ' '.join(
                [
                    'nova boot',
                    '--image', image,
                    '--flavor', flavor,
                    '--nic', 'net-id=' + net_id,
                    '--key-name', keypair_name,
                    # SSH authentication fails unless this is included.
                    '--config-drive', 'true',
                    # Wait for the machine to become active.
                    '--poll',
                    BUILDSLAVE_NODENAME
                ]
            )
        )
        run('nova list | grep {!r}'.format(BUILDSLAVE_NODENAME))


@task
def create_server(keypair_name):
    """
    Create a PistonCloud buildslave VM and wait for it to boot.
    Finally print its IP address.

    :param str keypair_name: The name of an SSH keypair that has been
        registered on the PistonCloud nova tenant.
    """
    # The alias for the openstack / nova administration server in
    # ``.ssh/config``.
    env.hosts = ['pistoncloud-novahost']
    execute(_create_server, keypair_name)


def _delete_server():
    """
    Call ``nova delete`` to delete the server on which the PistonCloud
    build slave is running.
    """
    with shell_env(OS_TENANT_NAME=TENANT_NAME):
        run('nova delete ' + BUILDSLAVE_NODENAME)


@task
def delete_server():
    """
    Delete the PistonCloud buildslave VM.
    """
    # The alias for the openstack / nova administration server in
    # ``.ssh/config``.
    env.hosts = ['pistoncloud-novahost']
    execute(_delete_server)


def _configure(index, password, master='build.staging.clusterhq.com'):
    """
    Install all the packages required by ``buildslave`` and then
    configure the PistonCloud buildslave.
    """
    # The default DNS servers on our PistonCloud tenant prevent
    # resolution of public DNS names.
    # Instead use Google's public DNS servers for the duration of the
    # build slave installation.
    set_google_dns()

    sudo("yum install -y epel-release")

    packages = [
        "https://kojipkgs.fedoraproject.org/packages/buildbot/0.8.10/1.fc22/noarch/buildbot-slave-0.8.10-1.fc22.noarch.rpm",  # noqa
        "git",
        "python",
        "python-devel",
        "python-tox",
        "python-virtualenv",
        "libffi-devel",
        "@buildsys-build",
        "openssl-devel",
        "wget",
        "curl",
        "enchant",
    ]

    sudo("yum install -y " + " ".join(packages))

    slashless_name = BUILDSLAVE_NAME.replace("/", "-") + '-' + str(index)
    builddir = 'builddir-' + str(index)

    sudo("mkdir -p {}".format(BUILDSLAVE_HOME))

    _configure_acceptance()

    sudo(
        u"buildslave create-slave "
        u"{buildslave_home}/{builddir} "
        u"{master} "
        u"{buildslave_name}-{index} "
        u"{password}".format(
            buildslave_home=BUILDSLAVE_HOME,
            builddir=builddir,
            master=master,
            buildslave_name=BUILDSLAVE_NAME,
            index=index,
            password=password,
        ),
    )

    put_template(
        template=FilePath(__file__).sibling('start.template'),
        replacements=dict(buildslave_home=BUILDSLAVE_HOME, builddir=builddir),
        remote_path=BUILDSLAVE_HOME + '/' + builddir + '/start',
        mode=0755,
        use_sudo=True,
    )

    remote_service_filename = slashless_name + '.service'

    put_template(
        template=FilePath(__file__).sibling('slave.service.template'),
        replacements=dict(
            buildslave_name=slashless_name,
            buildslave_home=BUILDSLAVE_HOME,
            builddir=builddir,
        ),
        remote_path=u'/etc/systemd/system/' + remote_service_filename,
        use_sudo=True,
    )

    sudo('systemctl start {}'.format(remote_service_filename))
    sudo('systemctl enable {}'.format(remote_service_filename))


@task
def configure(index, password, master='build.staging.clusterhq.com'):
    """
    Install and configure the buildslave on the PistonCloud buildslave
    VM.

    :param int index: The index of this PistonCloud build slave. You
        should probably just say 0.
    :param unicode password: The password that the build slave will
        use when authenticating with the build master.
    :param unicode master: The hostname or IP address of the build
        master that this build slave will attempt to connect to.
    """
    # The alias for the build slave server in ``.ssh/config``.
    env.hosts = ['pistoncloud-buildslave']
    execute(_configure, index, password, master)
