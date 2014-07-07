import os
import sys
import ast
import copy
import thread
import errno

try: import simplejson as json
except: import json

import cherrypy
from lxml import etree
from binascii import hexlify
from cherrypy import _cptools
from mako.template import Template
import time

TWISTER_PATH = os.getenv('TWISTER_PATH')
if not TWISTER_PATH:
    print('TWISTER_PATH environment variable is not set! Exiting!')
    exit(1)
if TWISTER_PATH not in sys.path:
    sys.path.append(TWISTER_PATH)

from common.tsclogging import *
from common.helpers    import *
from CeCommonAllocator import CommonAllocator

RESOURCE_FREE     = 1
RESOURCE_BUSY     = 2
RESOURCE_RESERVED = 3

constant_dictionary = {'version': 0, 'name': '/', 'meta': {}, 'children': {}}

def xml_to_res(xml, gparams, skip_header = False):

    # this is a recursive method to read the xml and generate a dictionary
    def recursive_xml_to_res(xml,res_dict):
        for folder in xml.findall('folder'):
            tb_path = folder.find('path')
            if tb_path is not None:
                nd = {'path':[],'meta': {}, 'id': '', 'children': {}}
                nd['path'].append(tb_path.text)
            else:
                nd = {'meta': {}, 'id': '', 'children': {}}

            # Populate META properties
            meta = folder.find('meta')
            if meta is not None:
                for meta_params in meta.findall('param'):
                    meta_name = meta_params.find('name')
                    if meta_name is not None:
                        meta_value = meta_params.find('value')
                        if meta_value is not None and meta_value.text is not None:
                            nd['meta'][meta_name.text] = meta_value.text
                        else:
                            nd['meta'][meta_name.text] = ''

            # If the XML node contains an ID, use it; else, create a random ID
            tb_id = folder.find('id')
            if tb_id is not None:
                id_value = tb_id.find('value')
                if id_value is not None and id_value.text is not None:
                    nd['id'] = id_value.text
                else:
                    nd['id'] = hexlify(os.urandom(5))
            else:
                nd['id'] = hexlify(os.urandom(5))

            # Add children for this node
            res_dict[folder.find('fname').text] = nd
            recursive_xml_to_res(folder,res_dict[folder.find('fname').text]['children'])

    # we have to get the information at root level(path,meta,id,version) first
    # version is added only if it exists in xml;
    if not skip_header:
        root_dict = {'path':[], 'meta':{}, 'id':'', 'children':{}}
        tb_path = xml.find('path').text
        if tb_path:
            root_dict['path'].append(tb_path)
        meta = xml.find('meta')
        for meta_elem in meta:
           key = meta_elem.find('name').text
           val = meta_elem.find('value').text
           if val:
               root_dict['meta'][key] = val
           else:
               root_dict['meta'][key] = ''
        root_dict['id'] = xml.find('id').text
        if xml.find('version') is not None and xml.find('version').text is not None:
            root_dict['version'] = int(xml.find('version').text)
        #else:
        #    root_dict['version'] = ''

        gparams = root_dict

    # rest of the xml file can be read recursively
    recursive_xml_to_res(xml,gparams['children'])

    return gparams


def res_to_xml(parent_node, xml, skip_header = False):
    # The node is valid ?
    if not parent_node:
        return False

    # if we are at root level, we need to get path, meta, id and version fields
    if not skip_header:
        # path is a list with 0 or 1 elements
        path = etree.SubElement(xml,'path')
        if parent_node.get('path') is not None and len(parent_node.get('path')) == 1:
           path.text = parent_node.get('path')[0]
        else:
           path.text = ''

        meta = etree.SubElement(xml,'meta')
        # meta is a dictionary
        for k, v in parent_node.get('meta').iteritems():
            tag = etree.SubElement(meta, 'param')
            prop = etree.SubElement(tag, 'name')
            prop.text = str(k)
            val  = etree.SubElement(tag, 'value')
            if v:
                val.text = str(v)
            else:
                val.text = ''
            typ  = etree.SubElement(tag, 'type')
            typ.text = 'string'
            desc  = etree.SubElement(tag, 'desc')

        tb_id = etree.SubElement(xml,'id')
        tb_id.text = parent_node.get('id')
        # add version only if it exists in dictionary; the SUT
        # files don't have version
        if parent_node.get('version') is not None:
            version = etree.SubElement(xml,'version')
            version.text = str(parent_node.get('version'))

    # This node has children ?
    if not parent_node.get('children'):
        return False

    for node in sorted(parent_node['children'].keys()):
        nd = dict(parent_node['children'][node])

        # Create empty folder
        folder = etree.SubElement(xml, 'folder')
        # Folder fname
        fname = etree.SubElement(folder, 'fname')
        fname.text = node
        # Folder fdesc
        fdesc = etree.SubElement(folder, 'fdesc')

        # get the path if exists
        if nd.get('path'):
            path = etree.SubElement(folder, 'path')
            path.text = nd.get('path')[0]

        # get meta information
        meta = etree.SubElement(folder,'meta')
        if  type(nd['meta']) is dict :
            for k, v in nd['meta'].iteritems():
                tag = etree.SubElement(meta, 'param')
                prop = etree.SubElement(tag, 'name')
                prop.text = str(k)
                val  = etree.SubElement(tag, 'value')
                if v:
                    val.text = str(v)
                else:
                    val.text = ''
                typ  = etree.SubElement(tag, 'type')
                typ.text = 'string'
                desc  = etree.SubElement(tag, 'desc')

        # get the id
        if nd.get('id'):
            tag = etree.SubElement(folder, 'id')
            val  = etree.SubElement(tag, 'value')
            val.text = nd['id']
            typ  = etree.SubElement(tag, 'type')
            typ.text = 'string'
            desc  = etree.SubElement(tag, 'desc')

        ch = res_to_xml(nd, folder, True)

    return xml


class TestBeds(_cptools.XMLRPCController, CommonAllocator):

    def __init__(self, project):

        logInfo('Starting TestBeds Allocator...')
        ti = time.time()

        self.project = project

        self.resources = constant_dictionary
        self.reservedResources = dict()
        self.lockedResources = dict()
        self.acc_lock = thread.allocate_lock() # Task change lock
        self.ren_lock = thread.allocate_lock() # Rename lock
        self.imp_lock = thread.allocate_lock() # Import lock
        self.save_lock = thread.allocate_lock() # Save lock
        self.load_lock = thread.allocate_lock() # Save lock
        self.res_file = '{}/config/resources.json'.format(TWISTER_PATH)
        self._loadedUsers = dict()
        self.load_tb(v=True)

        logInfo('TestBeds Allocator initialization took `{:.4f}` sec.'.format(time.time()-ti))


    def load_tb(self, v=False):

        logDebug('CeTestBeds:load_tb {}'.format(v))

        with self.load_lock:
            if not self.resources.get('children'):
                self.resources = constant_dictionary

            # try to load test bed resources file
            try:
                f = open(self.res_file, 'r')
                self.resources = json.load(f)
                f.close() ; del f
                if v:
                    logDebug('RA: Devices loaded successfully for user.')
            except Exception as e:
                if v:
                    logError('RA: There are no devices to load for user {}!'.format(e))
        return self.resources


    def save_tb(self, props={}):
        '''
        Function used to write the changes on HDD.
        '''
        logFull('CeTestBeds:_save {}'.format(props))
        log = list()

        with self.save_lock:
            v = self.resources.get('version', 0)
            self.resources['version'] = v + 1
            try:
                logDebug('Saving test bed file.')
                with open(self.res_file, 'w') as f:
                    json.dump(self.resources, f, indent=4)
            except Exception as e:
                log.append(e)
        if log:
            logError(log)
            return '*ERROR* ' + str(log)

        return True


    @cherrypy.expose
    def get_tb(self, query, props={}):
        '''
        Get the current version of the tb modified and unsaved or
        the version from the disk.
        '''
        logDebug('CeTestBeds:get_tb {} {}'.format(query,props))
        user_info = self.user_info(props)

        result = None
        # If the resource is reserved, get the latest unsaved changes
        if user_info[0] in self.reservedResources:
            for i in range(len(self.reservedResources[user_info[0]].values())):
                result = self.get_resource(query, self.reservedResources[user_info[0]].values()[i])
                if isinstance(result, dict):
                    break
        # Or get it from the disk
        if not isinstance(result, dict):
            result = self.get_resource(query)

        if isinstance(result, dict):
            if ':' in query:
                meta  = query.split(':')[1]
                ret = result['meta'].get(meta, '')
                return ret
            else:
                return self.format_resource(result, query)
        return "*ERROR* we could not find this resource: {}".format(query)


    @cherrypy.expose
    def delete_tb(self, res_query, props={}):
        '''
        Permanently delete a resource.
        '''
        logDebug('CeTestBeds:delete_tb {} {}'.format(res_query,props))

        resources = self.resources
        user_info = self.user_info(props)

        # If no resources...
        if not resources.get('children'):
            msg = 'User {}: There are no resources defined !'.format(user_info[0])
            logError(msg)
            return "*ERROR* " + msg

        if ':' in res_query:
            meta      = res_query.split(':')[1]
            res_query = res_query.split(':')[0]
        else:
            meta = ''

        _isResourceReserved = self.is_resource_reserved(res_query)
        if _isResourceReserved and _isResourceReserved != user_info[0]:
            msg = 'User {}: Cannot create new component: The resource is reserved for {} !'.format(user_info[0], _isResourceReserved)
            logError(msg)
            return '*ERROR* ' + msg

        _isResourceLocked = self.is_resource_locked(res_query)
        if _isResourceLocked and _isResourceLocked != user_info[0]:
            msg = 'User {}: The resource is locked for {} !'.format(user_info[0], _isResourceLocked)
            logError(msg)
            return '*ERROR* ' + msg

        if meta:
            # the path for the resource that has to be modified (delete it or meta)
            result= self.get_reserved_resource(res_query, props)
            if not result:
                logDebug("This resource is not reserved")
                return False

            # we have to delete only the meta property
            correct_path = copy.deepcopy(result['path'])

            #modify meta for parent
            if len(result['path']) == 1:
                child = result
            # modify to a component
            else:
                base_path = "/".join(result['path'][1:])
                child = self.get_path(base_path, result)
            try:
                child['meta'].pop(meta)
            except:
                msg = "This meta that you entered thoes not exist {}".format(meta)
                logDebug(msg)
                return "false"
            child['path'] = correct_path

            ### my verification
            logDebug("after I delete this resource: {}".format(self.reservedResources))

            return "true"
        else:
            #we don't have meta, we delete the resource
            resource_node = self.get_resource(res_query)
            if isinstance(resource_node, dict) and ''.join(resource_node['path']) in self.resources['children']:
                # user wants to delete the entire TB
                if ''.join(resource_node['path']) in self.resources['children']:
                    self.resources['children'].pop(resource_node['path'][0])
                    issaved = self.save_tb(props)
                    if not issaved:
                        msg = "We could not save this TB: {}.".format(res_query)
                        logDebug(msg)
                        return "*ERROR* " + msg
            #user wants to delete a component of the TB
            else:
                reserved_node = self.get_reserved_resource(res_query, props)
                if not reserved_node:
                    logError('Cannot access reserved resource, path or ID `{}` !'.format(res_query))
                    return False
                #get the direct parent of the resource
                base_path = "/".join(reserved_node['path'][1:-1])
                extract_r = self.get_path(base_path, reserved_node)
                extract_r['children'].pop(reserved_node['path'][-1])
                extract_r['path'] = reserved_node['path'][:-1]
                ### my verification - to be deleted !!!
                logDebug("after I delete this resource: {}".format(self.reservedResources))

            return "true"


    @cherrypy.expose
    def rename_tb(self, query, new_name, props={}):
        '''
        Rename a resource.
        '''

        logDebug('CeTestBeds:rename_tb query = {} new_name = {} props = {}\n'.format(query, new_name, props))

        user_info = self.user_info(props)

        if ':' in query:
            meta      = query.split(':')[1]
            query = query.split(':')[0]
        else:
            meta = ''

        _isResourceReserved = self.is_resource_reserved(query)
        if _isResourceReserved and _isResourceReserved != user_info[0]:
            msg = 'User {}: Cannot create new component: The resource is reserved for {} !'.format(user_info[0], _isResourceReserved)
            logError(msg)
            return '*ERROR* ' + msg

        _isResourceLocked = self.is_resource_locked(query)
        if _isResourceLocked and _isResourceLocked != user_info[0]:
            msg = 'User {}: Reserve resource: The resource is locked for {} !'.format(user_info[0], _isResourceLocked)
            logError(msg)
            return '*ERROR* ' + msg

        if '/' in new_name or ':' in new_name:
            logError('New resource name cannot contain `/` or `:`!')
            return False

        # If no resources...
        if not self.resources.get('children'):
            msg = 'There are no resources defined !'
            logError(msg)
            return '*ERROR* ' + msg

        # Correct node path
        result = self.get_reserved_resource(query, props)
        if not result:
            logError('Cannot access reserved resource, path or ID `{}` !')
            return False

        if result['path'][-1] == new_name:
            logDebug('No changes have been made to `{}`.'.format( new_name))
            return True

        with self.ren_lock:
            # If must rename a Meta info
            if meta:
                try:
                    # modify meta to the parent
                    if len(result['path']) == 1:
                        child = result
                    # modify to a component
                    else:
                        base_path = "/".join(result['path'][1:])
                        child = self.get_path(base_path, result)

                    child['meta'][new_name] = child['meta'].pop(meta)
                except:
                    msg = "This meta that you entered thoes not exist {}".format(meta)
                    logDebug(msg)
                    return "false"
                return self.save_reserved_tb(query, props)
            # If must rename a normal node
            else:
                # the parent is directly from the root and we want to rename its immediate children
                if len(result['path']) == 2:
                    result['children'][new_name] = result['children'].pop(result['path'][-1])
                # the component we want to rename is deep in the tree
                elif len(result['path']) > 2:
                    base_path = "/".join(result['path'][1:-1])
                    parent = self.get_path(base_path, result)
                    parent['children'][new_name] = parent['children'].pop(result['path'][-1])
                else:
                    result['path'] = [new_name]
                # we only have to change the current path and the path of the children
                result['path'] = [result['path'][0]]
                self.change_path(result, result['path'])

        return True


    @cherrypy.expose
    def create_component_tb(self, name, parent=None, props={}):
        '''
        Create a component for an existing TB.
        Return new component's id.
        '''

        user_info = self.user_info(props)

        props = self.valid_props(props)
        if parent == '/' or parent == '1':
            msg = "The parent value is not an existing TB. Mayebe you want to add a new TB. Parent: {}".format(parent)
            logError(msg)
            return "*ERROR* " + msg

        _isResourceReserved = self.is_resource_reserved(parent)
        if _isResourceReserved and _isResourceReserved != user_info[0]:
            msg = 'User {}: Cannot create new component: The resource is reserved for {} !'.format(user_info[0],_isResourceReserved)
            logError(msg)
            return '*ERROR* ' + msg

        _isResourceLocked = self.is_resource_locked(parent)
        if _isResourceLocked and _isResourceLocked != user_info[0]:
            msg = 'User {}: Reserve resource: The resource is locked for {} !'.format(user_info[0],_isResourceLocked)
            logError(msg)
            return '*ERROR* ' + msg

        with self.acc_lock:
            #the resource should be reserved previously
            parent_p = self.get_reserved_resource(parent, props)
            if not parent_p:
                msg = "Could not find this TB: '{}'".format(parent)
                logDebug(msg)
                return "*ERROR*" + msg

            #the resources is deep in the tree, we have to get its direct parent
            if len(parent_p['path']) >= 2:
                full_path = parent_p['path']
                base_path = "/".join(parent_p['path'][1:])
                parent_p = self.get_path(base_path, parent_p)
                parent_p['path'] = full_path

            if '/' in name:
                logDebug('Stripping slash characters from `{}`...'.format(name))
                name = name.replace('/', '')

            if name in parent_p['children']:
                msg = "A component with this name '{}' already exists for this TB: '{}'".format(name, parent)
                logDebug(msg)
                return "*ERROR*" + msg

            # the resource doesn't exist - create it
            res_id = self.generate_index()
            parent_p['children'][name] = {'id': res_id, 'meta': props, 'children': {}, 'path': parent_p['path'] + [name]}

            return res_id


    @cherrypy.expose
    def create_new_tb(self, name, parent=None, props={}):
        '''
        Create new test bed.
        Return the id of the new created tb.
        '''

        user_info = self.user_info(props)
        resources = self.resources

        if parent != '/' and parent != '1':
            msg = "The parent value is not root. Mayebe you want to add a component to an existing SUT. Parent: {}".format(parent)
            logError(msg)
            return "*ERROR* " + msg

        props = self.valid_props(props)

        with self.acc_lock:
            #root can not be reserved so we just take it
            parent_p = self.get_resource( '/', resources)

            if '/' in name:
                logDebug('Stripping slash characters from `{}`...'.format(name))
                name = name.replace('/', '')

            if name in self.resources['children']:
                msg = "A TB with this name '{}'' already exists'".format(name)
                logDebug(msg)
                return "*ERROR*" + msg

            # the resource doesn't exist - create it
            res_id = self.generate_index()
            parent_p['children'][name] = {'id': res_id, 'meta': props, 'children': {}, 'path': [name]}

            issaved = self.save_tb(props)
            if not issaved:
                msg = "We could not save this TB {}.".format(name)
                logDebug(msg)
                return "*ERROR* " + msg
            return res_id


    @cherrypy.expose
    def update_meta_tb(self, name, parent=None, props={}):
        '''
        Modify a resource, using a name, a parent Path or ID and some properties.
        '''
        logDebug('parent = {} -- props = {} -- name = {}'.format(parent, props, name))
        user_info = self.user_info(props)
        resources = self.resources

        props = self.valid_props(props)
        if not props or not self.valid_props(props):
            msg = "Wrong format for props = {}".format(props)
            logDebug(msg)
            return "*ERROR* " + msg

        if parent == "/" or parent == "1":
            #we can not reserve the root so we just take the TB we need
            if name[0] != "/":
                name = "/" + name
            verifyReserved = name
        else:
            #take the TB that has the component we need
            verifyReserved = parent

        _isResourceReserved = self.is_resource_reserved(verifyReserved)
        if _isResourceReserved and _isResourceReserved != user_info[0]:
            msg = 'User {}: Cannot create new component: The resource is reserved for {} !'.format(user_info[0],_isResourceReserved)
            logError(msg)
            return '*ERROR* ' + msg

        _isResourceLocked = self.is_resource_locked(verifyReserved)
        if _isResourceLocked and _isResourceLocked != user_info[0]:
            msg = 'User {}: Reserve resource: The resource is locked for {} !'.format(user_info[0],_isResourceLocked)
            logError(msg)
            return '*ERROR* ' + msg

        with self.acc_lock:

            # If this is the root resource, update the properties
            if name == '/' and parent == '/':
                resources['meta'].update(props)
                # Write changes for Device or SUT
                issaved = self.save_tb(props)
                if not issaved:
                    msg = "User {}: We didnt save this entry = {} having props = {}".format(user_info[0], name, props)
                    logDebug(msg)
                    return "*ERROR* " + msg
                return "true"

            parent_p = self.get_reserved_resource(verifyReserved, props)

            if not parent_p:
                logError('User {}: Cannot access reserved resource, path or ID `{}` !'.format(user_info[0], verifyReserved))
                return False

            #the resources is deep in the tree, we have to get its direct parent
            if len(parent_p['path']) >= 2:
                full_path = parent_p['path']
                base_path = "/".join(parent_p['path'][1:])
                parent_p = self.get_path(base_path, parent_p)
                parent_p['path'] = full_path

            if '/' in name:
                logDebug('User {}: Stripping slash characters from `{}`...'.format(user_info[0], name))
                name = name.replace('/', '')

            # the resource exists
            if name in parent_p['children']:
                child_p = parent_p['children'][name]
            elif name in parent_p['path']:
                child_p = parent_p
            else:
                return "User {}: *ERROR* the resource {} can not be found!".format(user_info[0], name)

            #We have to update the props
            child_p['meta'].update(props)

            return "true"



    @cherrypy.expose
    def reserve_tb(self, res_query, props={}):
        return self.reserve_resource(res_query, props)


    @cherrypy.expose
    def lock_tb(self, res_query, props={}):
        return self.lock_resource(res_query, props)


    @cherrypy.expose
    def unlock_tb(self, res_query, props={}):
        return self.unlock_resource(res_query, props)


    @cherrypy.expose
    def is_tb_locked(self, res_query):
        result = self.is_resource_locked(res_query)
        if not result:
            return "false"
        return result


    @cherrypy.expose
    def is_tb_reserved(self, res_query):
        result = self.is_resource_reserved(res_query)
        if not result:
            return "false"
        return result


    @cherrypy.expose
    def list_reserved_tb(self):
        return self.reservedResources


    @cherrypy.expose
    def list_locked_tb(self):
        return self.lockedResources


    @cherrypy.expose
    def list_all_tbs(self):
        """
        Fast list testbeds.
        """
        #maybe some resources changed meanwhile
        self.load_tb(v=False)

        res = []
        for k, v in self.resources.get('children').iteritems():
            path = [v.get('path')[0]] or []
            res.append(['/'.join(path), v['id']])
        result = []

        def quickFindPath(d, spath):
            for usr, locks in d.iteritems():
                for id, data in locks.iteritems():
                    path = data.get('path', [''])
                    if isinstance(path, str) or isinstance(path, unicode):
                        path = [path]
                    if path == [spath]:
                        return usr
            return None

        for tb, id in sorted(res):
            ruser = quickFindPath(self.reservedResources, tb)
            luser = quickFindPath(self.lockedResources, tb)

            if (not ruser) and (not luser):
                result.append({'id': id, 'name': tb, 'status': 'free'})
            elif ruser:
                result.append({'id': id, 'name': tb, 'status': 'reserved', 'user': ruser})
            elif luser:
                result.append({'id': id, 'name': tb, 'status': 'locked', 'user': luser})
            # Both reserved and locked ?
            else:
                result.append({'id': id, 'name': tb, 'status': 'reserved', 'user': ruser})

        user_info = self.user_info({})
        logDebug('User {}: Fast listing Resources... Found {}.'.format(user_info[0], res))

        return result


    @cherrypy.expose
    def export_tb_xml(self, xml_file, props={}):
        '''
        Export as XML file.
        '''
        user_info = self.user_info(props)

        logDebug('User {}: exporting to XML file `{}`'.format(user_info[0], xml_file))

        try:
            f = open(xml_file, 'w')
        except:
            msg = 'User {}: export XML: XML file `{}` cannot be written !'.format(user_info[0], xml_file)
            logError(msg)
            return '*ERROR* ' + msg

        logDebug('User {}: exporting to XML file `{}`...'.format(user_info[0], xml_file))

        xml = etree.Element('root')
        result = res_to_xml(self.resources, xml, False)
        f.write('<?xml version="1.0" encoding="UTF-8" standalone="no"?>\n\n')
        f.write(etree.tostring(xml, pretty_print=True))
        f.close()

        return True


    @cherrypy.expose
    def import_tb_xml(self, xml_file, props={}):
        '''
        Import one XML file.
        WARNING! This erases everything!
        '''
        user_info = self.user_info(props)
        self.resources = constant_dictionary

        logDebug('{} -- XML file `{}` !'.format(user_info[0], xml_file))
        if not os.path.isfile(xml_file):
            msg = 'User {} import XML: XML file `{}` does not exist!'.format(user_info[0], xml_file)
            logError(msg)
            return '*ERROR* ' + msg

        logDebug('User {}: importing XML file `{}`...'.format(user_info[0] ,xml_file))
        try:
            params_xml = etree.parse(xml_file)
        except:
                msg = "The file you selected: '{}' it's not an xml file. Try again!".format(xml_file)
                logDebug(msg)
                return '*ERROR* ' + msg

        with self.imp_lock:
            try:
                self.resources = xml_to_res(params_xml, {})
            except Exception as e:
                msg = 'User {}: Import XML: Exception `{}`.'.format(user_info[0], e)
                logError(msg)
                return '*ERROR* ' + msg
        # Write changes for Device or SUT
        issaved = self.save_tb(props)
        if isinstance(issaved, str):
            logDebug("We could not save this TB.")
            return False

        return True


    @cherrypy.expose
    def save_reserved_tb(self, res_query, props={}):
        """
        User has made some changes only on self.reserved_resources.
        In this method we sync self.reserved_resources with self.resources
        and the store on the disk
        """

        logDebug('CeTestBeds:save_reserved_tb {}'.format(res_query))

        user_info = self.user_info(props)
        resources = self.resources

        # If no resources...
        if not resources.get('children'):
            msg = 'User {}: Save reserved resource: There are no resources defined !'.format(user_info[0])
            logError(msg)
            return '*ERROR* ' + msg

        user_info = self.user_info(props)

        if ':' in res_query:
            res_query = res_query.split(':')[0]

        if not self.reservedResources.get(user_info[0]):
            msg = "User {}: It seems that this user does not have changes to save!".format(user_info[0])
            logError(msg)
            return "*ERROR* " + msg

        reserved_resources = copy.deepcopy(self.reservedResources.get(user_info[0]))


        resource_node = self.get_resource(res_query)
        if not resource_node or "*ERROR*" in resource_node:
            msg = "User {}: Can not find this resource = {}".format(user_info[0], res_query)
            logError(msg)
            return "*ERROR* " + msg

        if len(resource_node['path']) > 1:
            resource_node = self.get_path(resource_node['path'][0], resources)

        if not resource_node:
            msg = "User {}: We didnt find this resource {}".format(user_info[0], res_query)
            logDebug(msg)
            return "*ERROR* " + msg

        reserved_node = reserved_resources[resource_node['id']]

        # maybe the user renamed the TB
        if (reserved_node != resource_node):
            self.resources['children'][reserved_node['path'][0]] = self.resources['children'].pop(resource_node['path'][0])
        # or maybe the name of the resource is the same
        resources['children'].update([(reserved_node['path'][0], reserved_node), ])

        # update path
        resources['children'][reserved_node['path'][0]]['path'] = [reserved_node['path'][0]]

        #now we have to save
        issaved = self.save_tb(props)
        if isinstance(issaved, str) and issaved.startswith('*ERROR*'):
            msg = "We could not save this TB for user = {}.".format(user_info[0])
            logDebug(msg)
            return "*ERROR* " + msg
        return "true"


    @cherrypy.expose
    def save_release_reserved_tb(self, res_query, props={}):
        """
        Save the changes. Sync self.resources with self.reserved_resources
        and save to the disk
        """
        logDebug('CeTestBeds:save_release_reserved_tb {} {}'.format(res_query, props))

        # save changes
        result = self.save_reserved_tb(res_query, props)

        if result:
            user_info = self.user_info(props)

            if ':' in res_query:
                res_query = res_query.split(':')[0]

            # get only the component
            resource_node = self.get_resource(res_query)
            # get the entire TB
            if len(resource_node['path']) > 1:
                resource_node = self.get_path(resource_node['path'][0], resources)

            # delete this entry from reservedResources
            reserved_node = self.reservedResources[user_info[0]][resource_node['id']]
            self.reservedResources[user_info[0]].pop(reserved_node['id'])
            if not self.reservedResources[user_info[0]]:
                self.reservedResources.pop(user_info[0])
        else:
            return result

        return True

    @cherrypy.expose
    def discard_release_reserved_tb(self, res_query):
        return self.discard_release_reserved_resource(res_query)