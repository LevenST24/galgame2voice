package org.example.springai.config;

import org.springframework.context.annotation.Configuration;
import org.springframework.web.servlet.config.annotation.ResourceHandlerRegistry;
import org.springframework.web.servlet.config.annotation.WebMvcConfigurer;

import java.nio.file.Paths;

@Configuration
public class WebConfig implements WebMvcConfigurer {

    @Override
    public void addResourceHandlers(ResourceHandlerRegistry registry) {
        String audioDir = Paths.get("audio").toAbsolutePath().toUri().toString();
        registry.addResourceHandler("/audio/**")
                .addResourceLocations(audioDir);
    }
}