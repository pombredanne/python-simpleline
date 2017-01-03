# Base classes for Simpleline Text UI framework.
#
# Copyright (C) 2016  Red Hat, Inc.
#
# This copyrighted material is made available to anyone wishing to use,
# modify, copy, or redistribute it subject to the terms and conditions of
# the GNU General Public License v.2, or (at your option) any later version.
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY expressed or implied, including the implied warranties of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU General
# Public License for more details.  You should have received a copy of the
# GNU General Public License along with this program; if not, write to the
# Free Software Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA
# 02110-1301, USA.  Any Red Hat trademarks that are incorporated in the
# source code or documentation are not subject to the GNU General Public
# License and may only be used or replicated with the express permission of
# Red Hat, Inc.
#
# Author(s): Jiri Konecny <jkonecny@redhat.com>
#

__all__ = ["App", "UIScreen"]

import sys
import queue
import getpass
import threading
from simpleline.communication.communication import hubQ
from simpleline.utils.i18n import _, N_, C_
from simpleline.widgets import Widget, TextWidget
from simpleline.prompt import Prompt

RAW_INPUT_LOCK = threading.Lock()


def send_exception(queue_instance, ex):
    queue_instance.put((hubQ.HUB_CODE_EXCEPTION, [ex]))


class ExitMainLoop(Exception):
    """This exception ends the outermost mainloop. Used internally when dialogs close."""
    pass


class ExitAllMainLoops(ExitMainLoop):
    """This exception ends the whole App mainloop structure.

    App.run() returns False after the exception is processed.
    """
    pass


class App(object):
    """This is the main class for TUI screen handling.

    It is responsible for mainloop control and keeping track of the screen stack.

    Screens are organized in stack structure so it is possible to return
    to caller when dialog or sub-screen closes.

    It supports four window transitions:
    - show new screen replacing the current one (linear progression)
    - show new screen keeping the current one in stack (hub & spoke)
    - show new screen and wait for it to end (dialog)
    - close current window and return to the next one in stack
    """
    START_MAINLOOP = True
    STOP_MAINLOOP = False
    NOP = None

    _current_screen = None

    def __init__(self, title, quit_screen=None, width=80, queue_instance=None,
                 quit_message=None):
        """
        :param title: application title for whenever we need to display app name
        :type title: str

        :param quit_screen: UIScreen object class used for Quit dialog
        :type quit_screen: class UIScreen accepting additional message arg

        :param width: screen width for rendering purposes
        :type width: int

        :param queue_instance: if specified use this message queue for communication
        :type queue_instance: queue.Queue()

        :param quit_message: this message will be send to quit_screen
        :type quit_message: str
        """
        self._header = title
        self._redraw = True
        self._spacer = "\n".join(2 * [width * "="])
        self._width = width
        self._input_thread = None
        self.quit_screen = quit_screen
        self.quit_message = quit_message or N_(u"Do you really want to quit?")

        # async control queue
        if queue_instance:
            self.queue_instance = queue_instance
        else:
            self.queue_instance = queue.Queue()

        # event handlers
        # key: event id
        # value: list of tuples (callback, data)
        self._handlers = {}

        # screen stack contains triplets
        #  UIScreen to show
        #  arguments for it's show method
        #  value indicating whether new mainloop is needed
        #   - None = do nothing
        #   - True = execute new loop
        #   - False = already running loop, exit when window closes
        self._screens = []

    def register_event_handler(self, event, callback, data=None):
        """This method registers a callback which will be called when message "event"
        is encountered during process_events.

        The callback has to accept two arguments:
        - the received message in the form of (type, [arguments])
        - the data registered with the handler

        :param event: the id of the event we want to react on
        :type event: number|string

        :param callback: the callback function
        :type callback: func(event_message, data)

        :param data: optional data to pass to callback
        :type data: anything
        """
        if not event in self._handlers:
            self._handlers[event] = []
        self._handlers[event].append((callback, data))

    def _thread_input(self, queue_instance, prompt, hidden):
        """This method is responsible for interruptible user input.

        It is expected to be used in a thread started on demand by the App class
        and returns the input via the communication Queue.

        :param queue_instance: communication queue_instance to be used
        :type queue_instance: queue.Queue instance

        :param prompt: prompt to be displayed
        :type prompt: Prompt instance

        :param hidden: whether typed characters should be echoed or not
        :type hidden: bool
        """
        if hidden:
            data = getpass.getpass(prompt)
        else:
            widget = TextWidget(str(prompt))
            widget.render(self.width)
            lines = widget.get_lines()
            sys.stdout.write("\n".join(lines) + " ")
            sys.stdout.flush()
            # XXX: only one raw_input can run at a time, don't schedule another
            # one as it would cause weird behaviour and block other packages'
            # raw_inputs
            if not RAW_INPUT_LOCK.acquire(False):
                # raw_input is already running
                return
            else:
                # lock acquired, we can run raw_input
                try:
                    data = input()
                except EOFError:
                    data = ""
                finally:
                    RAW_INPUT_LOCK.release()

        queue_instance.put((hubQ.HUB_CODE_INPUT, [data]))

    def switch_screen(self, ui, args=None):
        """Schedules a screen to replace the current one.

        :param ui: screen to show
        :type ui: instance of UIScreen

        :param args: optional argument to pass to ui's refresh method (can be used to select what item should be displayed or so)
        :type args: anything
        """
        # (oldscr, oldattr, oldloop)
        oldloop = self._screens.pop()[2]

        # we have to keep the oldloop value so we stop
        # dialog's mainloop if it ever uses switch_screen
        self._screens.append((ui, args, oldloop))
        self.redraw()

    def switch_screen_with_return(self, ui, args=None):
        """Schedules a screen to show, but keeps the current one in stack to return to, when the new one is closed.

        :param ui: screen to show
        :type ui: UIScreen instance

        :param args: optional argument, please see switch_screen for details
        :type args: anything
        """
        self._screens.append((ui, args, self.NOP))

        self.redraw()

    def switch_screen_modal(self, ui, args=None):
        """Starts a new screen right away, so the caller can collect data back.

        When the new screen is closed, the caller is redisplayed.

        This method does not return until the new screen is closed.

        :param ui: screen to show
        :type ui: UIScreen instance

        :param args: optional argument, please see switch_screen for details
        :type args: anything
        """
        # set the third item to True so new loop gets started
        self._screens.append((ui, args, self.START_MAINLOOP))
        self._do_redraw()

    def schedule_screen(self, ui, args=None):
        """Add screen to the bottom of the stack.

        This is mostly useful at the beginning to prepare the first screen hierarchy to display.

        :param ui: screen to show
        :type ui: UIScreen instance

        :param args: optional argument, please see switch_screen for details
        :type args: anything
        """
        self._screens.insert(0, (ui, args, self.NOP))

    def close_screen(self, scr=None):
        """Close the currently displayed screen and exit it's main loop if necessary.

        Next screen from the stack is then displayed.

        :param scr: if an UIScreen instance is passed it is checked to be the screen we are trying to close.
        :type scr: UIScreen instance
        """
        oldscr, _oldattr, oldloop = self._screens.pop()
        if scr is not None:
            assert oldscr == scr

        # this cannot happen, if we are closing the window,
        # the loop must have been running or not be there at all
        assert oldloop != self.START_MAINLOOP

        # we are in modal window, end it's loop
        if oldloop == self.STOP_MAINLOOP:
            raise ExitMainLoop()

        if self._screens:
            self.redraw()
        else:
            raise ExitMainLoop()

    def _do_redraw(self):
        """Draws the current screen and returns True if user input is requested.

        If modal screen is requested, starts a new loop and initiates redraw after it ends.

        :return: this method returns True if user input processing is requested
        :rtype: bool
        """
        # there is nothing to display, exit
        if not self._screens:
            raise ExitMainLoop()

        # get the screen from the top of the stack
        screen, args, newloop = self._screens[-1]
        self.current_screen = screen

        # new mainloop is requested
        if newloop == self.START_MAINLOOP:
            # change the record to indicate mainloop is running
            self._screens.pop()
            self._screens.append((screen, args, self.STOP_MAINLOOP))
            # start the mainloop
            self._mainloop()
            # after the mainloop ends, set the redraw flag
            # and skip the input processing once, to redisplay the screen first
            self.redraw()
            input_needed = False
        elif self._redraw:
            # get the widget tree from the screen and show it in the screen
            try:
                input_needed = screen.refresh(args)
                screen.show_all()
                self._redraw = False
            except ExitMainLoop:
                raise
            except Exception:    # pylint: disable=broad-except
                send_exception(self.queue_instance, sys.exc_info())
                return False

        else:
            # this can happen only in case there was invalid input and prompt
            # should be shown again
            input_needed = True

        return input_needed

    def run(self):
        """This methods starts the application.

        Do not use self.mainloop() directly as run() handles all the required exceptions
        needed to keep nested mainloops working.
        """
        try:
            self._mainloop()
            return True
        except ExitAllMainLoops:
            return False

    def _mainloop(self):
        """Single mainloop. Do not use directly, start the application using run()."""
        # ask for redraw by default
        self._redraw = True

        # initial state
        last_screen = None
        error_counter = 0

        # run until there is nothing else to display
        while self._screens:
            # process asynchronous events
            self.process_events()

            # if redraw is needed, separate the content on the screen from the
            # stuff we are about to display now
            if self._redraw:
                print(self._spacer)

            try:
                # draw the screen if redraw is needed or the screen changed
                # (unlikely to happen separately, but just be sure)
                if self._redraw or last_screen != self._screens[-1][0]:
                    # we have fresh screen, reset error counter
                    error_counter = 0
                    if not self._do_redraw():
                        # if no input processing is requested, go for another cycle
                        continue

                last_screen = self._screens[-1][0]

                # get the screen's prompt
                try:
                    prompt = last_screen.prompt(self._screens[-1][1])
                except ExitMainLoop:
                    raise
                except Exception:    # pylint: disable=broad-except
                    send_exception(self.queue_instance, sys.exc_info())
                    continue

                # None means prompt handled the input by itself
                # ask for redraw and continue
                if prompt is None:
                    self.redraw()
                    continue

                # get the input from user
                c = self.raw_input(prompt)

                # process the input, if it wasn't processed (valid)
                # increment the error counter
                if not self.input(self._screens[-1][1], c):
                    error_counter += 1
                else:
                    # input was successfully processed, but no other screen was
                    # scheduled, just redraw the screen to display current state
                    self.redraw()

                # redraw the screen after 5 bad inputs
                if error_counter >= 5:
                    self.redraw()

            # propagate higher to end all loops
            # not really needed here, but we might need
            # more processing in the future
            except ExitAllMainLoops:
                raise

            # end just this loop
            except ExitMainLoop:
                break

    def application_quit_cb(self):
        """This callback will be called when user quits the application.
        This is mainly for overriding purposes.
        """
        pass

    def process_events(self, return_at=None):
        """This method processes incoming async messages and returns
        when a specific message is encountered or when the queue_instance
        is empty.

        If return_at message was specified, the received message is returned.

        If the message does not fit return_at, but handlers are
        defined then it processes all handlers for this message
        """
        while return_at or not self.queue_instance.empty():
            event = self.queue_instance.get()
            if event[0] == return_at:
                return event
            elif event[0] in self._handlers:
                for handler, data in self._handlers[event[0]]:
                    try:
                        handler(event, data)
                    except ExitMainLoop:
                        raise
                    except Exception:    # pylint: disable=broad-except
                        send_exception(self.queue_instance, sys.exc_info())

    def raw_input(self, prompt, hidden=False):
        """This method reads one input from user. Its basic form has only one
        line, but we might need to override it for more complex apps or testing.
        """
        if self._input_thread is not None and self._input_thread.is_alive():
            raise KeyError("Can't run multiple input threads at the same time!")

        self._input_thread = threading.Thread(target=self._thread_input, name="InputThread",
                                              args=(self.queue_instance, prompt, hidden))
        self._input_thread.daemon = True
        self._input_thread.start()
        event = self.process_events(return_at=hubQ.HUB_CODE_INPUT)
        return event[1][0]  # return the user input

    def input(self, args, key):
        """Method called internally to process unhandled input key presses.

        Also handles the main quit and close commands.

        :param args: optional argument passed from switch_screen calls
        :type args: anything

        :param key: the string entered by user
        :type key: str

        :return: True if key was processed, False if it was not recognized
        :rtype: True|False
        """
        # delegate the handling to active screen first
        if self._screens:
            try:
                key = self._screens[-1][0].input(args, key)
                if key is None:
                    return True
            except ExitMainLoop:
                raise
            except Exception:    # pylint: disable=broad-except
                send_exception(self.queue_instance, sys.exc_info())
                return False

        # global refresh command
        # TRANSLATORS: 'r' to refresh
        if self._screens and (key == C_('TUI|Spoke Navigation', 'r')):
            self._do_redraw()
            return True

        # global close command
        # TRANSLATORS: 'c' to continue
        if self._screens and (key == C_('TUI|Spoke Navigation', 'c')):
            self.close_screen()
            return True

        # global quit command
        # TRANSLATORS: 'q' to quit
        elif self._screens and (key == C_('TUI|Spoke Navigation', 'q')):
            if self.quit_screen:
                d = self.quit_screen(self, _(self.quit_message))
                self.switch_screen_modal(d)
                if d.answer:
                    self.application_quit_cb()
                    raise ExitAllMainLoops()
            else:
                self.application_quit_cb()
                raise ExitAllMainLoops()
            return True

        return False

    def redraw(self):
        """Set the redraw flag so the screen is refreshed as soon as possible."""
        self._redraw = True

    @property
    def header(self):
        return self._header

    @property
    def width(self):
        """Return the total width of screen space we have available."""
        return self._width

    @property
    def current_screen(self):
        """Get the currently visible TUI screen."""
        return App._current_screen

    @current_screen.setter
    def current_screen(self, new_screen):
        """Set the currently visible TUI screen.

        Why are we using App._current_screen and not self._current_screen ?

        There can actually be multiple App instances (the AskVNCSpoke for example
        has a different App instance than the SummaryHub in Anaconda), but there can
        still be only one screen displayed at once.
        So we use the class variable and simply track what screen is the last displayed
        regardless of App instance.
        """
        # is this a new screen or still the same one ?
        if new_screen != App._current_screen:
            # in some cases we run simple dialogs that are not full spokes
            # and thus lack the entry() & exit() spoke methods, so we need to check
            # for that

            # "close" the previous screen (if any)
            if App._current_screen and hasattr(App._current_screen, "exit"):
                App._current_screen.exit()
            # "enter" the new screen (if any)
            if new_screen and hasattr(new_screen, "entry"):
                new_screen.entry()

        App._current_screen = new_screen


class UIScreen(object):
    """Base class representing one TUI Screen.

    Shares some API with anaconda's GUI to make it easy for devs to create similar UI
    with the familiar API.
    """
    # title line of the screen
    title = u"Screen.."

    def __init__(self, app, screen_height=25):
        """
        :param app: reference to application main class
        :type app: instance of class App

        :param screen_height: height of the screen (useful for printing long widgets)
        :type screen_height: int
        """
        self._app = app
        self._screen_height = screen_height

        # list that holds the content to be printed out
        self._window = []

        # index of the page (subset of screen) shown during show_all
        # indexing starts with 0
        self._page = 0

    def setup(self, *args):
        """Do additional setup right before this screen is used.

        :param args: arguments for the setup
        :type args: array of values
        :return: whether this screen should be scheduled or not
        :rtype: bool
        """
        return True

    def refresh(self, args=None):
        """Method which prepares the content desired on the screen to self._window.

        :param args: optional argument passed from switch_screen calls
        :type args: anything

        :return: has to return True if input processing is requested, otherwise
                 the screen will get printed and the main loop will continue
        :rtype: True|False
        """
        self._window = [_(self.title), u""]
        return True

    def _print_long_widget(self, widget):
        """Prints a long widget (possibly longer than the screen height) with user interaction (when needed).

        :param widget: possibly long widget to print
        :type widget: Widget instance
        """
        pos = 0
        lines = widget.get_lines()
        num_lines = len(lines)

        if num_lines < self._screen_height - 2:
            # widget plus prompt are shorter than screen height, just print the widget
            print(u"\n".join(lines))
            return

        # long widget, print it in steps and prompt user to continue
        last_line = num_lines - 1
        while pos <= last_line:
            if pos + self._screen_height - 2 > last_line:
                # enough space to print the rest of the widget plus regular
                # prompt (2 lines)
                for line in lines[pos:]:
                    print(line)
                pos += self._screen_height - 1
            else:
                # print part with a prompt to continue
                for line in lines[pos:(pos + self._screen_height - 2)]:
                    print(line)
                self._app.raw_input(Prompt(_("\nPress %s to continue") % Prompt.ENTER))
                pos += self._screen_height - 1

    def show_all(self):
        """Prepares all elements of self._window for output and then prints them on the screen."""
        for w in self._window:
            if hasattr(w, "render"):
                w.render(self.app.width)                    # pylint: disable=no-member
            if isinstance(w, Widget):
                self._print_long_widget(w)
            elif isinstance(w, bytes):
                print(w)
            else:
                # not a widget or string, just print its string representation
                print(str(w))
    show = show_all

    def hide(self):
        """This does nothing in TUI, it is here to make API similar."""
        pass

    def input(self, args, key):
        """Method called to process input. If the input is not handled here, return it.

        :param key: input string to process
        :type key: str

        :param args: optional argument passed from switch_screen calls
        :type args: anything

        :return: return True or INPUT_PROCESSED (None) if key was handled,
                 INPUT_DISCARDED (False) if the screen should not process input
                 on the App and key if you want it to.
        :rtype: True|False|None|str
        """
        return key

    def prompt(self, args=None):
        """Return the text to be shown as prompt or handle the prompt and return None.

        :param args: optional argument passed from switch_screen calls
        :type args: anything

        :return: returns an instance of Prompt with text to be shown next to the prompt
                 for input or None to skip further input processing
        :rtype: Prompt instance|None
        """
        prompt = Prompt()
        prompt.add_refresh_option()
        prompt.add_continue_option()
        prompt.add_quit_option()
        return prompt

    @property
    def app(self):
        """The reference to this Screen's assigned App instance."""
        return self._app

    def close(self):
        """Close the current screen."""
        self.app.close_screen(self)



if __name__ == "__main__":
    class HelloWorld(UIScreen):
        def show(self, args=None):
            print("""Hello World\nquit by typing 'quit'""")
            return True

    a = App("Hello World")
    s = HelloWorld(a, None)
    a.schedule_screen(s)
    a.run()
