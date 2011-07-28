# Copyright 2011 OpenStack LLC.
# All Rights Reserved.
#
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

import base64
import re
import webob

from webob import exc
from xml.dom import minidom

from nova import exception
from nova import flags
from nova import log as logging
import nova.image
from nova import quota
from nova import utils

from nova.compute import instance_types
from nova.api.openstack import wsgi
from nova.auth import manager as auth_manager


LOG = logging.getLogger('nova.api.openstack.create_instance_helper')
FLAGS = flags.FLAGS


class CreateFault(exception.NovaException):
    message = _("Invalid parameters given to create_instance.")

    def __init__(self, fault):
        self.fault = fault
        super(CreateFault, self).__init__()


class CreateInstanceHelper(object):
    """This is the base class for OS API Controllers that
    are capable of creating instances (currently Servers and Zones).

    Once we stabilize the Zones portion of the API we may be able
    to move this code back into servers.py
    """

    def __init__(self, controller):
        """We need the image service to create an instance."""
        self.controller = controller
        self._image_service = utils.import_object(FLAGS.image_service)
        super(CreateInstanceHelper, self).__init__()

    def create_instance(self, req, body, create_method):
        """Creates a new server for the given user. The approach
        used depends on the create_method. For example, the standard
        POST /server call uses compute.api.create(), while
        POST /zones/server uses compute.api.create_all_at_once().

        The problem is, both approaches return different values (i.e.
        [instance dicts] vs. reservation_id). So the handling of the
        return type from this method is left to the caller.
        """
        if not body:
            raise exc.HTTPUnprocessableEntity()

        if not 'server' in body:
            raise exc.HTTPUnprocessableEntity()

        server_dict = body['server']
        context = req.environ['nova.context']
        password = self.controller._get_server_admin_password(server_dict)

        key_name = None
        key_data = None
        key_pairs = auth_manager.AuthManager.get_key_pairs(context)
        if key_pairs:
            key_pair = key_pairs[0]
            key_name = key_pair['name']
            key_data = key_pair['public_key']

        image_href = self.controller._image_ref_from_req_data(body)
        try:
            image_service, image_id = nova.image.get_image_service(image_href)
            kernel_id, ramdisk_id = self._get_kernel_ramdisk_from_image(
                                                req, image_id)
            images = set([str(x['id']) for x in image_service.index(context)])
            assert str(image_id) in images
        except Exception, e:
            msg = _("Cannot find requested image %(image_href)s: %(e)s" %
                                                                    locals())
            raise exc.HTTPBadRequest(explanation=msg)

        personality = server_dict.get('personality')

        injected_files = []
        if personality:
            injected_files = self._get_injected_files(personality)

        try:
            flavor_id = self.controller._flavor_id_from_req_data(body)
        except ValueError as error:
            msg = _("Invalid flavorRef provided.")
            raise exc.HTTPBadRequest(explanation=msg)

        if not 'name' in server_dict:
            msg = _("Server name is not defined")
            raise exc.HTTPBadRequest(explanation=msg)

        zone_blob = server_dict.get('blob')
        name = server_dict['name']
        self._validate_server_name(name)
        name = name.strip()

        reservation_id = server_dict.get('reservation_id')
        min_count = server_dict.get('min_count')
        max_count = server_dict.get('max_count')
        # min_count and max_count are optional.  If they exist, they come
        # in as strings.  We want to default 'min_count' to 1, and default
        # 'max_count' to be 'min_count'.
        min_count = int(min_count) if min_count else 1
        max_count = int(max_count) if max_count else min_count
        if min_count > max_count:
            min_count = max_count

        try:
            inst_type = \
                    instance_types.get_instance_type_by_flavor_id(flavor_id)
            extra_values = {
                'instance_type': inst_type,
                'image_ref': image_href,
                'password': password}

            return (extra_values,
                    create_method(context,
                                  inst_type,
                                  image_id,
                                  kernel_id=kernel_id,
                                  ramdisk_id=ramdisk_id,
                                  display_name=name,
                                  display_description=name,
                                  key_name=key_name,
                                  key_data=key_data,
                                  metadata=server_dict.get('metadata', {}),
                                  injected_files=injected_files,
                                  admin_password=password,
                                  zone_blob=zone_blob,
                                  reservation_id=reservation_id,
                                  min_count=min_count,
                                  max_count=max_count))
        except quota.QuotaError as error:
            self._handle_quota_error(error)
        except exception.ImageNotFound as error:
            msg = _("Can not find requested image")
            raise exc.HTTPBadRequest(explanation=msg)
        except exception.FlavorNotFound as error:
            msg = _("Invalid flavorRef provided.")
            raise exc.HTTPBadRequest(explanation=msg)
        # Let the caller deal with unhandled exceptions.

    def _handle_quota_error(self, error):
        """
        Reraise quota errors as api-specific http exceptions
        """
        if error.code == "OnsetFileLimitExceeded":
            expl = _("Personality file limit exceeded")
            raise exc.HTTPBadRequest(explanation=expl)
        if error.code == "OnsetFilePathLimitExceeded":
            expl = _("Personality file path too long")
            raise exc.HTTPBadRequest(explanation=expl)
        if error.code == "OnsetFileContentLimitExceeded":
            expl = _("Personality file content too long")
            raise exc.HTTPBadRequest(explanation=expl)
        # if the original error is okay, just reraise it
        raise error

    def _deserialize_create(self, request):
        """
        Deserialize a create request

        Overrides normal behavior in the case of xml content
        """
        if request.content_type == "application/xml":
            deserializer = ServerXMLDeserializer()
            return deserializer.deserialize(request.body)
        else:
            return self._deserialize(request.body, request.get_content_type())

    def _validate_server_name(self, value):
        if not isinstance(value, basestring):
            msg = _("Server name is not a string or unicode")
            raise exc.HTTPBadRequest(explanation=msg)

        if value.strip() == '':
            msg = _("Server name is an empty string")
            raise exc.HTTPBadRequest(explanation=msg)

    def _get_kernel_ramdisk_from_image(self, req, image_id):
        """Fetch an image from the ImageService, then if present, return the
        associated kernel and ramdisk image IDs.
        """
        context = req.environ['nova.context']
        image_meta = self._image_service.show(context, image_id)
        # NOTE(sirp): extracted to a separate method to aid unit-testing, the
        # new method doesn't need a request obj or an ImageService stub
        kernel_id, ramdisk_id = self._do_get_kernel_ramdisk_from_image(
            image_meta)
        return kernel_id, ramdisk_id

    @staticmethod
    def  _do_get_kernel_ramdisk_from_image(image_meta):
        """Given an ImageService image_meta, return kernel and ramdisk image
        ids if present.

        This is only valid for `ami` style images.
        """
        image_id = image_meta['id']
        if image_meta['status'] != 'active':
            raise exception.ImageUnacceptable(image_id=image_id,
                                              reason=_("status is not active"))

        if image_meta.get('container_format') != 'ami':
            return None, None

        try:
            kernel_id = image_meta['properties']['kernel_id']
        except KeyError:
            raise exception.KernelNotFoundForImage(image_id=image_id)

        try:
            ramdisk_id = image_meta['properties']['ramdisk_id']
        except KeyError:
            raise exception.RamdiskNotFoundForImage(image_id=image_id)

        return kernel_id, ramdisk_id

    def _get_injected_files(self, personality):
        """
        Create a list of injected files from the personality attribute

        At this time, injected_files must be formatted as a list of
        (file_path, file_content) pairs for compatibility with the
        underlying compute service.
        """
        injected_files = []

        for item in personality:
            try:
                path = item['path']
                contents = item['contents']
            except KeyError as key:
                expl = _('Bad personality format: missing %s') % key
                raise exc.HTTPBadRequest(explanation=expl)
            except TypeError:
                expl = _('Bad personality format')
                raise exc.HTTPBadRequest(explanation=expl)
            try:
                contents = base64.b64decode(contents)
            except TypeError:
                expl = _('Personality content for %s cannot be decoded') % path
                raise exc.HTTPBadRequest(explanation=expl)
            injected_files.append((path, contents))
        return injected_files

    def _get_server_admin_password_old_style(self, server):
        """ Determine the admin password for a server on creation """
        return utils.generate_password(16)

    def _get_server_admin_password_new_style(self, server):
        """ Determine the admin password for a server on creation """
        password = server.get('adminPass')

        if password is None:
            return utils.generate_password(16)
        if not isinstance(password, basestring) or password == '':
            msg = _("Invalid adminPass")
            raise exc.HTTPBadRequest(explanation=msg)
        return password


class ServerXMLDeserializer(wsgi.MetadataXMLDeserializer):
    """
    Deserializer to handle xml-formatted server create requests.

    Handles standard server attributes as well as optional metadata
    and personality attributes
    """

    def action(self, string):
        dom = minidom.parseString(string)
        action_node = dom.childNodes[0]
        action_name = action_node.tagName

        action_deserializer = {
            'createImage': self._action_create_image,
            'createBackup': self._action_create_image,
        }.get(action_name, self.default)

        action_data = action_deserializer(action_node)

        return {'body': {action_name: action_data}}

    def _action_create_image(self, node):
        data = {}
        value = node.getAttribute('name')
        if value:
            data['name'] = value
        metadata_node = self.find_first_child_named(node, 'metadata')
        data['metadata'] = self.extract_metadata(metadata_node)
        return data

    def _action_create_image(self, node):
        data = {}
        attributes = ['name', 'backup_type', 'rotation']
        for attribute in attributes:
            value = node.getAttribute(attribute)
            if value:
                data[attribute] = value
        metadata_node = self.find_first_child_named(node, 'metadata')
        data['metadata'] = self.extract_metadata(metadata_node)
        return data

    def create(self, string):
        """Deserialize an xml-formatted server create request"""
        dom = minidom.parseString(string)
        server = self._extract_server(dom)
        return {'body': {'server': server}}

    def _extract_server(self, node):
        """Marshal the server attribute of a parsed request"""
        server = {}
        server_node = self.find_first_child_named(node, 'server')

        attributes = ["name", "imageId", "flavorId", "imageRef",
                     "flavorRef", "adminPass"]
        for attr in attributes:
            if server_node.getAttribute(attr):
                server[attr] = server_node.getAttribute(attr)

        metadata_node = self.find_first_child_named(server_node, "metadata")
        server["metadata"] = self.extract_metadata(metadata_node)

        server["personality"] = self._extract_personality(server_node)

        return server

    def _extract_personality(self, server_node):
        """Marshal the personality attribute of a parsed request"""
        node = self.find_first_child_named(server_node, "personality")
        personality = []
        if node is not None:
            for file_node in self.find_children_named(node, "file"):
                item = {}
                if file_node.hasAttribute("path"):
                    item["path"] = file_node.getAttribute("path")
                item["contents"] = self.extract_text(file_node)
                personality.append(item)
        return personality
