# Copyright 2019 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the 'License');
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an 'AS IS' BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import sys
import svgwrite
import threading

import gi
gi.require_version('Gst', '1.0')
gi.require_version('GstBase', '1.0')
gi.require_version('Gtk', '3.0')
from gi.repository import GLib, GObject, Gst, GstBase, Gtk

GObject.threads_init()
Gst.init(None)

class GstPipeline:
    def __init__(self, pipeline, user_function, src_size):
        self.user_function = user_function
        self.running = False
        self.gstbuffer = None
        self.sink_size = None
        self.src_size = src_size
        self.box = None
        self.condition = threading.Condition()

        self.pipeline = Gst.parse_launch(pipeline)
        self.overlay = self.pipeline.get_by_name('overlay')
        self.overlaysink = self.pipeline.get_by_name('overlaysink')
        appsink = self.pipeline.get_by_name('appsink')
        appsink.connect('new-sample', self.on_new_sample)

        # Set up a pipeline bus watch to catch errors.
        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect('message', self.on_bus_message)

        # Set up a full screen window on Coral, no-op otherwise.
        self.setup_window()

    def run(self):
        # Start inference worker.
        self.running = True
        worker = threading.Thread(target=self.inference_loop)
        worker.start()

        # Run pipeline.
        self.pipeline.set_state(Gst.State.PLAYING)
        try:
            Gtk.main()
        except:
            pass

        # Clean up.
        self.pipeline.set_state(Gst.State.NULL)
        while GLib.MainContext.default().iteration(False):
            pass
        with self.condition:
            self.running = False
            self.condition.notify_all()
        worker.join()

    def on_bus_message(self, bus, message):
        t = message.type
        if t == Gst.MessageType.EOS:
            Gtk.main_quit()
        elif t == Gst.MessageType.WARNING:
            err, debug = message.parse_warning()
            sys.stderr.write('Warning: %s: %s\n' % (err, debug))
        elif t == Gst.MessageType.ERROR:
            err, debug = message.parse_error()
            sys.stderr.write('Error: %s: %s\n' % (err, debug))
            Gtk.main_quit()
        return True

    def on_new_sample(self, sink):
        sample = sink.emit('pull-sample')
        if not self.sink_size:
            s = sample.get_caps().get_structure(0)
            self.sink_size = (s.get_value('width'), s.get_value('height'))
        with self.condition:
            self.gstbuffer = sample.get_buffer()
            self.condition.notify_all()
        return Gst.FlowReturn.OK

    def get_box(self):
        if not self.box:
            glbox = self.pipeline.get_by_name('glbox')
            if glbox:
                glbox = glbox.get_by_name('filter')
            box = self.pipeline.get_by_name('box')
            assert glbox or box
            assert self.sink_size
            if glbox:
                self.box = (glbox.get_property('x'), glbox.get_property('y'),
                        glbox.get_property('width'), glbox.get_property('height'))
            else:
                self.box = (-box.get_property('left'), -box.get_property('top'),
                    self.sink_size[0] + box.get_property('left') + box.get_property('right'),
                    self.sink_size[1] + box.get_property('top') + box.get_property('bottom'))
        return self.box

    def inference_loop(self):
        while True:
            with self.condition:
                while not self.gstbuffer and self.running:
                    self.condition.wait()
                if not self.running:
                    break
                gstbuffer = self.gstbuffer
                self.gstbuffer = None

            # Passing Gst.Buffer as input tensor avoids 2 copies of it:
            # * Python bindings copies the data when mapping gstbuffer
            # * Numpy copies the data when creating ndarray.
            # This requires a recent version of the python3-edgetpu package. If this
            # raises an exception please make sure dependencies are up to date.
            input_tensor = gstbuffer
            svg = self.user_function(input_tensor, self.src_size, self.get_box())
            if svg:
                if self.overlay:
                    self.overlay.set_property('data', svg)
                if self.overlaysink:
                    self.overlaysink.set_property('svg', svg)

    def setup_window(self):
        # Only set up our own window if we have Coral overlay sink in the pipeline.
        if not self.overlaysink:
            return

        gi.require_version('GstGL', '1.0')
        gi.require_version('GstVideo', '1.0')
        from gi.repository import GstGL, GstVideo

        # Needed to commit the wayland sub-surface.
        def on_gl_draw(sink, widget):
            widget.queue_draw()

        # Needed to account for window chrome etc.
        def on_widget_configure(widget, event, overlaysink):
            allocation = widget.get_allocation()
            overlaysink.set_render_rectangle(allocation.x, allocation.y,
                    allocation.width, allocation.height)
            return False

        window = Gtk.Window(Gtk.WindowType.TOPLEVEL)
        window.fullscreen()

        drawing_area = Gtk.DrawingArea()
        window.add(drawing_area)
        drawing_area.realize()

        self.overlaysink.connect('drawn', on_gl_draw, drawing_area)

        # Wayland window handle.
        wl_handle = self.overlaysink.get_wayland_window_handle(drawing_area)
        self.overlaysink.set_window_handle(wl_handle)

        # Wayland display context wrapped as a GStreamer context.
        wl_display = self.overlaysink.get_default_wayland_display_context()
        self.overlaysink.set_context(wl_display)

        drawing_area.connect('configure-event', on_widget_configure, self.overlaysink)
        window.connect('delete-event', Gtk.main_quit)
        window.show_all()

        # The appsink pipeline branch must use the same GL display as the screen
        # rendering so they get the same GL context. This isn't automatically handled
        # by GStreamer as we're the ones setting an external display handle.
        def on_bus_message_sync(bus, message, overlaysink):
            if message.type == Gst.MessageType.NEED_CONTEXT:
                _, context_type = message.parse_context_type()
                if context_type == GstGL.GL_DISPLAY_CONTEXT_TYPE:
                    sinkelement = overlaysink.get_by_interface(GstVideo.VideoOverlay)
                    gl_context = sinkelement.get_property('context')
                    if gl_context:
                        display_context = Gst.Context.new(GstGL.GL_DISPLAY_CONTEXT_TYPE, True)
                        GstGL.context_set_gl_display(display_context, gl_context.get_display())
                        message.src.set_context(display_context)
            return Gst.BusSyncReply.PASS

        bus = self.pipeline.get_bus()
        bus.set_sync_handler(on_bus_message_sync, self.overlaysink)

def detectCoralDevBoard():
  try:
    if 'MX8MQ' in open('/sys/firmware/devicetree/base/model').read():
      print('Detected Edge TPU dev board.')
      return True
  except: pass
  return False

def run_pipeline(user_function, appsink_size):
    # For default camera (Coral Camera w/ Dev Board):
    PIPELINE = 'v4l2src device=/dev/video0 ! {src_caps}'
    # For alternative camera (USB camera w/ Dev Board):
    #PIPELINE = 'v4l2src device=/dev/video1 ! {src_caps}'
    SRC_CAPS = 'video/x-raw,width={width},height={height},framerate=30/1'
    if detectCoralDevBoard():
        scale_caps = None
        src_size = (1920, 1080)
        PIPELINE += """ ! glupload ! tee name=t
            t. ! queue ! glfilterbin filter=glbox name=glbox ! {sink_caps} ! {sink_element}
            t. ! queue ! glsvgoverlaysink name=overlaysink
        """
    else:
        src_size = (640, 480)
        scale = min(appsink_size[0] / src_size[0], appsink_size[1] / src_size[1])
        scale = tuple(int(x * scale) for x in src_size)
        scale_caps = 'video/x-raw,width={width},height={height}'.format(width=scale[0], height=scale[1])
        PIPELINE += """ ! tee name=t
            t. ! {leaky_q} ! videoconvert ! videoscale ! {scale_caps} ! videobox name=box autocrop=true
               ! {sink_caps} ! {sink_element}
            t. ! {leaky_q} ! videoconvert
               ! rsvgoverlay name=overlay ! videoconvert ! ximagesink sync=false
            """

    SINK_ELEMENT = 'appsink name=appsink emit-signals=true max-buffers=1 drop=true'
    SINK_CAPS = 'video/x-raw,format=RGB,width={width},height={height}'
    LEAKY_Q = 'queue max-size-buffers=1 leaky=downstream'

    src_caps = SRC_CAPS.format(width=src_size[0], height=src_size[1])
    sink_caps = SINK_CAPS.format(width=appsink_size[0], height=appsink_size[1])
    pipeline = PIPELINE.format(leaky_q=LEAKY_Q,
        src_caps=src_caps, sink_caps=sink_caps,
        sink_element=SINK_ELEMENT, scale_caps=scale_caps)

    print('Gstreamer pipeline:\n', pipeline)

    pipeline = GstPipeline(pipeline, user_function, src_size)
    pipeline.run()
