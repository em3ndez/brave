from gi.repository import Gst
from brave.inputoutputoverlay import InputOutputOverlay
from brave.mixers.source_collection import SourceCollection
import brave.config as config
from brave.helpers import unblock_pad


class Mixer(InputOutputOverlay):
    '''
    An abstract superclass representing a mixer.
    A mixer takes video and/or audio inputs and allows them to be mixed
    (including overlaying to make e.g. picture-in-picture).
    '''

    def __init__(self, **args):
        args['type'] = 'mixer'
        super().__init__(**args)
        self.mixer_element = {}
        self.request_pad_count = {'video': 0, 'audio': 0}
        self.sources = SourceCollection(self)
        self.create_elements()

        # Set initially to READY, and when there we set to self.props['initial_state']
        self.set_state(Gst.State.READY)

    def permitted_props(self):
        return {
            **super().permitted_props(),
            'width': {
                'type': 'int',
                'default': config.default_mixer_width()
            },
            'height': {
                'type': 'int',
                'default': config.default_mixer_height()
            },
            'pattern': {
                'type': 'int',
                'default': 0
            },
        }

    def input_output_overlay_or_mixer(self):
        return 'mixer'

    def summarise(self):
        s = super().summarise()
        s['sources'] = self.sources.get_as_pretty_object()
        return s

    def add_element(self, factory_name, who_its_for, name=None):
        '''
        Add an element on the pipeline belonging to this mixer.
        '''
        if name is None:
            name = factory_name
        e = Gst.ElementFactory.make(factory_name, who_its_for.input_output_overlay_or_mixer() +
                                    '_' + str(who_its_for.id) + '_' + name)
        if not e:
            raise Exception('Unable to make GStreamer element "' + str(factory_name) +
                            '" - the most likely reason is it is not installed.')
        self.pipeline.add(e)
        return e

    def create_elements(self):
        '''
        Create the initial elements needed for this mixer.
        '''
        pipeline_string = ''
        if config.enable_video():
            # To work reliably we have a default source (videotestsrc)
            # It has the lowest permitted zorder (0) so that other things will appear on top.
            # After the compositor, the format is changed from RGBA to RGBx (i.e. remove the alpha chanel)
            # This is done (a) for overlay effects to work, and (b) for all outputs to work.
            pipeline_string += ('videotestsrc is-live=true name=videotestsrc ! videoconvert ! videoscale ! '
                                'capsfilter name=capsfilter ! compositor name=video_mixer ! '
                                'video/x-raw,format=RGBA ! videoconvert ! queue name=video_mixer_output_queue ! '
                                'capsfilter name=end_capsfilter caps="video/x-raw,format=RGBx" ! videoconvert ! '
                                'tee name=final_video_tee allow-not-linked=true')
        if config.enable_audio():
            pipeline_string += \
                f' audiotestsrc is-live=true volume=0 ! {config.default_audio_caps()} ! ' + \
                'queue name=audio_queue ! audiomixer name=audio_mixer ! ' + \
                'tee name=final_audio_tee allow-not-linked=true'

        if not self.create_pipeline_from_string(pipeline_string):
            return False

        self.end_capsfilter = self.pipeline.get_by_name('end_capsfilter')

        if config.enable_video():
            self.videotestsrc = self.pipeline.get_by_name('videotestsrc')
            self.mixer_element['video'] = self.pipeline.get_by_name('video_mixer')
            self.video_mixer_output_queue = self.pipeline.get_by_name('video_mixer_output_queue')
            self.final_video_tee = self.pipeline.get_by_name('final_video_tee')
            self.capsfilter = self.pipeline.get_by_name('capsfilter')
            self._set_dimensions()
            self.handle_updated_props()
            self.session().overlays.ensure_overlays_are_correctly_connected(self)

        if config.enable_audio():
            self.mixer_element['audio'] = self.pipeline.get_by_name('audio_mixer')
            self.audio_mixer_output_queue = self.pipeline.get_by_name('audio_mixer_output_queue')
            self.final_audio_tee = self.pipeline.get_by_name('final_audio_tee')

        return True

    def on_pipeline_start(self):
        '''
        Called when the stream starts
        '''
        # Tell each output to unblock its intervideosrc as content is now coming through
        for name, output in self.session().outputs.items():
            if output.get_state() in [Gst.State.PLAYING, Gst.State.PAUSED]:
                unblock_pad(output, 'intervideosrc_src_pad')
                unblock_pad(output, 'interaudiosrc_src_pad')

        # Likewise, tell each input
        for source in self.sources:
            source.unblock_intersrc_if_ready()

    def get_dimensions(self):
        '''
        Get the width and height of this mix.
        '''
        return self.props['width'], self.props['height']

    def get_new_pad_for_source(self, audio_or_video):
        '''
        Get a new pad from the mixer, to add a new source
        '''
        self.request_pad_count[audio_or_video] += 1
        return self.mixer_element[audio_or_video].get_request_pad('sink_%d' % self.request_pad_count[audio_or_video])

    def handle_updated_props(self):
        if 'pattern' in self.props:
            self.videotestsrc.set_property('pattern', self.props['pattern'])

    def _set_dimensions(self):
        dimensions_caps_string = 'video/x-raw,width=%s,height=%s' % (self.props['width'], self.props['height'])
        self.logger.debug('Dimensions caps: ' + dimensions_caps_string)
        dimensions_caps = Gst.Caps.from_string(dimensions_caps_string)
        self.capsfilter.set_property('caps', dimensions_caps)
