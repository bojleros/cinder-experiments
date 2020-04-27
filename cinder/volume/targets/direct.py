#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import abc

from oslo_concurrency import processutils
from oslo_log import log as logging

from cinder import exception
from cinder.i18n import _
from cinder import utils
from cinder.volume.targets import driver
from cinder.volume import volume_utils

LOG = logging.getLogger(__name__)


class DirectTarget(driver.Target):
    """Target object for block storage devices.

    Base class for target object, where target
    is data transport mechanism (target) specific calls.
    This includes things like create targets, attach, detach
    etc.
    """

    def __init__(self, *args, **kwargs):
        super(DirectTarget, self).__init__(*args, **kwargs)
        self.protocol = 'local'
        self.volumes_dir = self.configuration.safe_get('volumes_dir')

    def create_export(self, context, volume, volume_path):
        """Creates an export for a logical volume."""
        LOG.debug("Create export on volume %s in path %s" %
                  (volume, volume_path))
        data = dict()
        data['device_path'] = volume_path
        data['auth'] = None
        data['location'] = volume.availability_zone

        return data

    def remove_export(self, context, volume):
        LOG.debug("Unexport on volume %s" % volume)

    def ensure_export(self, context, volume, volume_path):
        """Recreates an export for a logical volume."""
        LOG.debug("Ensure export on volume %s in path %s" %
                  (volume, volume_path))

    def initialize_connection(self, volume, connector):
        """Initializes the connection and returns connection info.
        """

        return {
            'driver_volume_type': self.protocol,
            'data': {'device_path': "/dev/%s/volume-%s" % (self.configuration.safe_get('volume_group'), volume.id)}
        }

    def terminate_connection(self, volume, connector, **kwargs):
        pass

    def validate_connector(self, connector):
        # NOTE(jdg): api passes in connector which is initiator info
        if 'initiator' not in connector:
            err_msg = ('The volume driver requires the iSCSI initiator '
                       'name in the connector.')
            LOG.error(err_msg)
            raise exception.InvalidConnectorException(missing='initiator')
        return True

    def extend_target(self, volume):
        """Reinitializes a target after the LV has been extended.

        Note: This will cause IO disruption in most cases.
        """
        LOG.debug("Extend target on volume %s" %
                  (volume))

    def _get_target_and_lun(self, context, volume):
        """Get iscsi target and lun."""
        pass

    def create_iscsi_target(self, name, tid, lun, path,
                            chap_auth, **kwargs):
        pass

    def remove_iscsi_target(self, tid, lun, vol_id, vol_name, **kwargs):
        pass

    def _get_iscsi_target(self, context, vol_id):
        pass

    def _get_target(self, iqn):
        pass
