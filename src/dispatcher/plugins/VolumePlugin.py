#+
# Copyright 2014 iXsystems, Inc.
# All rights reserved
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted providing that the following conditions
# are met:
# 1. Redistributions of source code must retain the above copyright
#    notice, this list of conditions and the following disclaimer.
# 2. Redistributions in binary form must reproduce the above copyright
#    notice, this list of conditions and the following disclaimer in the
#    documentation and/or other materials provided with the distribution.
#
# THIS SOFTWARE IS PROVIDED BY THE AUTHOR ``AS IS'' AND ANY EXPRESS OR
# IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
# WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED.  IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR ANY
# DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
# DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS
# OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION)
# HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT,
# STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING
# IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.
#
#####################################################################

import errno
import os
from task import Provider, Task, ProgressTask, TaskException, VerifyException, query
from dispatcher.rpc import RpcException, description, accepts, returns
from utils import first_or_default


VOLUMES_ROOT = '/volumes'


def flatten_datasets(root):
    for ds in root['children']:
        for c in flatten_datasets(ds):
            yield c

    del root['children']
    yield root


@description("Provides access to volumes information")
class VolumeProvider(Provider):
    @query('definitions/volume')
    def query(self, filter=None, params=None):
        result = []
        single = params.pop('single', False) if params else False
        for vol in self.datastore.query('volumes', *(filter or []), **(params or {})):
            config = self.get_config(vol['name'])
            if not config:
                vol['status'] = 'UNKNOWN'
            else:
                topology = config['groups']
                for vdev, _ in iterate_vdevs(topology):
                    vdev['path'] = self.dispatcher.call_sync('disk.partition_to_disk', vdev['path'])

                vol['topology'] = topology
                vol['status'] = config['status']
                vol['properties'] = config['properties']
                vol['datasets'] = list(flatten_datasets(config['root_dataset']))

            if single:
                return vol

            result.append(vol)

        return result

    def find(self):
        result = []
        for pool in self.dispatcher.call_sync('zfs.pool.find'):
            topology = pool['groups']
            for vdev, _ in iterate_vdevs(topology):
                vdev['path'] = self.dispatcher.call_sync('disk.partition_to_disk', vdev['path'])

            result.append({
                'id': str(pool['guid']),
                'name': pool['name'],
                'topology': topology,
                'status': pool['status']
            })

        return result

    def resolve_path(self, path):
        volname, _, rest = path.partition(':')
        volume = self.query([('name', '=', volname)], {'single': True})
        if not volume:
            raise RpcException(errno.ENOENT, 'Volume {0} not found'.format(volname))

        return os.path.join(volume['mountpoint'], rest)

    def decode_path(self, path):
        path = os.path.normpath(path)[1:]
        tokens = path.split(os.sep)

        if tokens[0] != 'volumes':
            raise RpcException(errno.EINVAL, 'Invalid path')

        volname = tokens[1]
        config = self.get_config(volname)
        datasets = map(lambda d: d['name'], flatten_datasets(config['root_dataset']))
        n = len(tokens)

        while n > 0:
            fragment = '/'.join(tokens[1:n])
            if fragment in datasets:
                return volname, fragment, '/'.join(tokens[n:])

            n -= 1

        raise RpcException(errno.ENOENT, 'Cannot look up path')

    def get_volume_disks(self, name):
        result = []
        for dev in self.dispatcher.call_sync('zfs.pool.get_disks', name):
            result.append(self.dispatcher.call_sync('disk.partition_to_disk', dev))

        return result

    def get_available_disks(self):
        disks = set([d['path'] for d in self.dispatcher.call_sync('disk.query')])
        for pool in self.dispatcher.call_sync('zfs.pool.query'):
            for dev in self.dispatcher.call_sync('zfs.pool.get_disks', pool['name']):
                disk = self.dispatcher.call_sync('disk.partition_to_disk', dev)
                disks.remove(disk)

        return list(disks)

    def get_config(self, volume):
        return self.dispatcher.call_sync('zfs.pool.query', [('name', '=', volume)], {'single': True})

    def get_capabilities(self, type):
        if type == 'zfs':
            return self.dispatcher.call_sync('zfs.pool.get_capabilities')

        raise RpcException(errno.EINVAL, 'Invalid volume type')


@description("Creates new volume")
@accepts({
    'type': 'string',
    'title': 'name'
}, {
    'type': 'string',
    'title': 'type'
}, {
    'type': 'object',
    'title': 'topology',
    'properties': {
        'groups': {'type': 'object'}
    }
})
class VolumeCreateTask(ProgressTask):
    def __get_disks(self, topology):
        for vdev, gname in iterate_vdevs(topology):
            yield vdev['path'], gname

    def __get_disk_gptid(self, disk):
        config = self.dispatcher.call_sync('disk.get_disk_config', disk)
        return config.get('data-partition-path', disk)

    def __convert_topology_to_gptids(self, topology):
        topology = topology.copy()
        for vdev, _ in iterate_vdevs(topology):
            vdev['path'] = self.__get_disk_gptid(vdev['path'])

        return topology

    def verify(self, name, type, topology, params=None):
        if self.datastore.exists('volumes', ('name', '=', name)):
            raise VerifyException(errno.EEXIST, 'Volume with same name already exists')

        return ['disk:{0}'.format(i) for i, _ in self.__get_disks(topology)]

    def run(self, name, type, topology, params=None):
        subtasks = []
        params = params or {}
        mountpoint = params.pop('mountpoint', os.path.join(VOLUMES_ROOT, name))

        for dname, dgroup in self.__get_disks(topology):
            subtasks.append(self.run_subtask('disk.format.gpt', dname, 'freebsd-zfs', {
                'blocksize': params.get('blocksize', 4096),
                'swapsize': params.get('swapsize') if dgroup == 'data' else 0
            }))

        self.set_progress(10)
        self.join_subtasks(*subtasks)
        self.set_progress(40)
        self.join_subtasks(self.run_subtask('zfs.pool.create', name, self.__convert_topology_to_gptids(topology)))
        self.set_progress(60)
        self.join_subtasks(self.run_subtask('zfs.mount', name))
        self.set_progress(80)

        pool = self.dispatcher.call_sync('zfs.pool.query', [('name', '=', name)]).pop()
        id = self.datastore.insert('volumes', {
            'id': str(pool['guid']),
            'name': name,
            'type': type,
            'mountpoint': mountpoint
        })

        self.set_progress(90)
        self.dispatcher.dispatch_event('volumes.changed', {
            'operation': 'create',
            'ids': [id]
        })


@description("Creates new volume and automatically guesses disks layout")
@accepts({
    'type': 'string',
    'title': 'name'
}, {
    'type': 'string',
    'title': 'type'
}, {
    'type': 'array',
    'title': 'disks',
    'items': {'type': 'string'}
})
class VolumeAutoCreateTask(VolumeCreateTask):
    def verify(self, name, type, disks, params=None):
        if self.datastore.exists('volumes', ('name', '=', name)):
            raise VerifyException(errno.EEXIST, 'Volume with same name already exists')

        return ['disk:{0}'.format(i) for i in disks]

    def run(self, name, type, disks, params=None):
        vdevs = []
        if len(disks) % 3 == 0:
            for i in xrange(0, len(disks), 3):
                vdevs.append({
                    'type': 'raidz',
                    'children': [{'type': 'disk', 'path': os.path.join('/dev', i)} for i in disks[i:i+3]]
                })
        elif len(disks) % 2 == 0:
            for i in xrange(0, len(disks), 2):
                vdevs.append({
                    'type': 'mirror',
                    'children': [{'type': 'disk', 'path': os.path.join('/dev', i)} for i in disks[i:i+2]]
                })
        else:
            vdevs = [{'type': 'disk', 'path': os.path.join('/dev', i)} for i in disks]

        self.join_subtasks(self.run_subtask('volume.create', name, type, {'data': vdevs}, params))


class VolumeDestroyTask(Task):
    def verify(self, name):
        if not self.datastore.exists('volumes', ('name', '=', name)):
            raise VerifyException(errno.ENOENT, 'Volume {0} not found'.format(name))

        return ['disk:{0}'.format(d) for d in self.dispatcher.call_sync('volumes.get_volume_disks', name)]

    def run(self, name):
        vol = self.datastore.get_one('volumes', ('name', '=', name))
        self.join_subtasks(self.run_subtask('zfs.umount', name))
        self.join_subtasks(self.run_subtask('zfs.pool.destroy', name))
        self.datastore.delete('volumes', vol['id'])

        self.dispatcher.dispatch_event('volumes.changed', {
            'operation': 'delete',
            'ids': [vol['id']]
        })


class VolumeUpdateTask(Task):
    def verify(self, name, updated_params):
        pass


class VolumeImportTask(Task):
    def verify(self, id, new_name, params=None):
        if self.datastore.exists('volumes', ('id', '=', id)):
            raise VerifyException(errno.ENOENT, 'Volume with id {0} already exists'.format(id))

        if self.datastore.exists('volumes', ('name', '=', new_name)):
            raise VerifyException(errno.ENOENT, 'Volume with name {0} already exists'.format(new_name))

        return self.verify_subtask('zfs.pool.import', id)

    def run(self, id, new_name, params=None):
        mountpoint = os.path.join(VOLUMES_ROOT, new_name)
        self.join_subtasks(self.run_subtask('zfs.pool.import', id, new_name, params))
        self.join_subtasks(self.run_subtask('zfs.configure', new_name, {'mountpoint': mountpoint}))
        self.join_subtasks(self.run_subtask('zfs.mount', new_name))

        new_id = self.datastore.insert('volumes', {
            'id': id,
            'name': new_name,
            'type': 'zfs',
            'mountpoint': mountpoint
        })

        self.dispatcher.dispatch_event('volumes.changed', {
            'operation': 'create',
            'ids': [new_id]
        })


class VolumeDetachTask(Task):
    def verify(self, name):
        if not self.datastore.exists('volumes', ('name', '=', name)):
            raise VerifyException(errno.ENOENT, 'Volume {0} not found'.format(name))

        return ['disk:{0}'.format(d) for d in self.dispatcher.call_sync('volumes.get_volume_disks', name)]

    def run(self, name):
        vol = self.datastore.get_one('volumes', ('name', '=', name))
        self.join_subtasks(self.run_subtask('zfs.umount', name))
        self.join_subtasks(self.run_subtask('zfs.pool.export', name))
        self.datastore.delete('volumes', vol['id'])

        self.dispatcher.dispatch_event('volumes.changed', {
            'operation': 'delete',
            'ids': [vol['id']]
        })


class DatasetCreateTask(Task):
    def verify(self, pool_name, path, params=None):
        if not self.datastore.exists('volumes', ('name', '=', pool_name)):
            raise VerifyException(errno.ENOENT, 'Volume {0} not found'.format(pool_name))

        return ['zpool:{0}'.format(pool_name)]

    def run(self, pool_name, path, params=None):
        self.join_subtasks(self.run_subtask('zfs.create_dataset', pool_name, path, params))
        self.join_subtasks(self.run_subtask('zfs.mount', path))


class DatasetDeleteTask(Task):
    def verify(self, pool_name, path):
        if not self.datastore.exists('volumes', ('name', '=', pool_name)):
            raise VerifyException(errno.ENOENT, 'Volume {0} not found'.format(pool_name))

        return ['zpool:{0}'.format(pool_name)]

    def run(self, pool_name, path):
        self.join_subtasks(self.run_subtask('zfs.umount', path))
        self.join_subtasks(self.run_subtask('zfs.destroy', pool_name, path))


class DatasetConfigureTask(Task):
    def verify(self, pool_name, path, updated_params):
        if not self.datastore.exists('volumes', ('name', '=', pool_name)):
            raise VerifyException(errno.ENOENT, 'Volume {0} not found'.format(pool_name))

        return ['zpool:{0}'.format(pool_name)]

    def run(self, pool_name, path, updated_params):
        pass


def iterate_vdevs(topology):
    for name, grp in topology.items():
        for vdev in grp:
            if vdev['type'] == 'disk':
                yield vdev, name
                continue

            if 'children' in vdev:
                for child in vdev['children']:
                    yield child, name


def _depends():
    return ['DevdPlugin', 'ZfsPlugin']


def _init(dispatcher):
    boot_pool = dispatcher.call_sync('zfs.pool.get_boot_pool')

    def on_pool_change(args):
        ids = filter(lambda i: i != boot_pool['guid'], args['ids'])

        if args['operation'] == 'delete':
            for i in args['ids']:
                dispatcher.datastore.delete('volumes', i)

        dispatcher.dispatch_event('volumes.changed', {
            'operation': args['operation'],
            'ids': ids
        })

    dispatcher.register_schema_definition('volume', {
        'type': 'object',
        'title': 'volume',
        'properties': {
            'name': {'type': 'string'},
            'topology': {'$ref': 'definitions/zfs-topology'},
            'params': {'type': 'object'}
        }
    })

    dispatcher.require_collection('volumes')
    dispatcher.register_provider('volumes', VolumeProvider)
    dispatcher.register_task_handler('volume.create', VolumeCreateTask)
    dispatcher.register_task_handler('volume.create_auto', VolumeAutoCreateTask)
    dispatcher.register_task_handler('volume.destroy', VolumeDestroyTask)
    dispatcher.register_task_handler('volume.import', VolumeImportTask)
    dispatcher.register_task_handler('volume.detach', VolumeDetachTask)
    dispatcher.register_task_handler('volume.dataset.create', DatasetCreateTask)
    dispatcher.register_task_handler('volume.dataset.delete', DatasetDeleteTask)
    dispatcher.register_task_handler('volume.dataset.update', DatasetConfigureTask)

    dispatcher.register_hook('volume.pre-destroy')
    dispatcher.register_hook('volume.pre-detach')
    dispatcher.register_hook('volume.pre-create')
    dispatcher.register_hook('volume.pre-attach')

    dispatcher.register_event_handler('zfs.pool.changed', on_pool_change)
    dispatcher.register_event_type('volumes.changed')